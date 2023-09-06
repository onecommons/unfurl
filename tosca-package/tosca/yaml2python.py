# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Converts a TOSCA service template from YAML to Python.

Repositories are resolved with by creating a`tosca_repository` directory with symlinks to the source.
Imports to followed and converted in-place.

Usage:

unfurl export --format python ensemble-template.yaml

The yaml converted to python will be removed and replace with an import statement that imports the python file.
"""
from dataclasses import MISSING
import logging
import os
from pathlib import Path
import pprint
import re
import string
import textwrap
import sys
import datetime
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    cast,
)
from toscaparser import functions
from toscaparser.imports import is_url, normalize_path
from toscaparser.elements.nodetype import NodeType
from toscaparser.elements.relationshiptype import RelationshipType
from toscaparser.elements.statefulentitytype import StatefulEntityType
from toscaparser.elements.property_definition import PropertyDef
from toscaparser.entity_template import EntityTemplate
from toscaparser.properties import Property
from toscaparser.elements.interfaces import OperationDef, _create_operations
from toscaparser.tosca_template import ToscaTemplate
from toscaparser.elements.entity_type import EntityType
from toscaparser.elements.datatype import DataType
from toscaparser.elements.artifacttype import ArtifactTypeDef
from toscaparser.elements.constraints import constraint_mapping, Schema
from toscaparser.elements.scalarunit import get_scalarunit_class
from keyword import iskeyword
import collections.abc
from . import _tosca
import black
import black.mode
import black.report


value_indent = 2


try:
    from ruamel.yaml.comments import CommentedMap

    def __repr__(self):
        return dict.__repr__(self)

    CommentedMap.__repr__ = __repr__  # type: ignore
    pprint.PrettyPrinter._pprint_ordered_dict = pprint.PrettyPrinter._pprint_dict  # type: ignore
    pprint.PrettyPrinter._dispatch[  # type: ignore
        CommentedMap.__repr__
    ] = pprint.PrettyPrinter._pprint_dict  # type: ignore
except ImportError:
    pass


def _make_typedef(
    typename: str, custom_defs: Dict[str, dict], all=False
) -> Optional[StatefulEntityType]:
    typedef = None
    # prefix is only used to expand "tosca:Type"
    test_typedef = StatefulEntityType(
        typename, StatefulEntityType.NODE_PREFIX, custom_defs
    )
    if test_typedef.is_derived_from("tosca.nodes.Root"):
        typedef = NodeType(typename, custom_defs)
    elif test_typedef.is_derived_from("tosca.relationships.Root"):
        typedef = RelationshipType(typename, custom_defs)
    elif all:
        if test_typedef.is_derived_from("tosca.artifacts.Root"):
            typedef = ArtifactTypeDef(typename, custom_defs)
        else:
            return test_typedef
    return typedef


def has_function(obj: object, seen=None) -> bool:
    if seen is None:
        seen = set()
    if id(obj) in seen:
        return False
    else:
        seen.add(id(obj))
    if functions.is_function(obj):
        return True
    elif isinstance(obj, collections.abc.Mapping):
        return any(has_function(i, seen) for i in obj.values())
    elif isinstance(obj, collections.abc.MutableSequence):
        return any(has_function(i, seen) for i in obj)
    return False


DT = TypeVar("DT", bound=_tosca.DataType)


def value2python_repr(value, quote=False) -> str:
    if sys.version_info.minor > 7:
        pprinted = pprint.pformat(value, compact=True, indent=value_indent, sort_dicts=False)  # type: ignore
    else:
        pprinted = pprint.pformat(value, compact=True, indent=value_indent)
    if not quote:
        if has_function(value):
            return f"Ref({pprinted})"
    return pprinted


def add_description(defs, indent):
    description = isinstance(defs, dict) and defs.get("description")
    if description:
        for q in ['"""', "'''", '"', '"']:
            # avoid """foo""""
            if q not in description and description[-1] != q[0]:
                quote = q
                break
        if "\n" in description:
            src = f"{indent}{quote}\n"
            src += textwrap.indent(description.rstrip(), indent)
            src += f"\n{indent}{quote}\n\n"
        else:
            src = f"{indent}{quote}{description}{quote}\n\n"
    else:
        src = ""
    return src


def metadata_repr(metadata) -> str:
    if sys.version_info.minor > 7:
        return pprint.pformat(metadata, indent=value_indent, sort_dicts=False)  # type: ignore
    else:
        return pprint.pformat(metadata, indent=value_indent)


def expand_prefix(nodetype: str):
    return nodetype.replace("tosca:", "tosca.nodes.")


def encode_identifier(name):
    def encode(match):
        return f"_{ord(match.group(0))}_"

    return re.sub(r"[^A-Za-z0-9_]", encode, name)


# def decode_identifier(name):
#     def decode(match):
#         return chr(int(match.group(1)))

#     return re.sub(r"_([0-9]+)_", decode, name)


def section2typename(section: str) -> str:
    return string.capwords(section, "_").replace("_", "")[:-1]


class Imports:
    def __init__(self, imports=None):
        self._imports: Dict[str, Tuple[str, Type[_tosca.ToscaType]]] = {}
        self._add_imports("", imports or {})
        self._import_statements = set()

    def _add_imports(self, basename: str, namespace: Dict[str, Any]):
        for name, ref in namespace.items():
            if basename:
                qname = basename + "." + name
            else:
                qname = name
            if not isinstance(ref, type):
                continue
            elif issubclass(ref, _tosca.Namespace):
                self._add_imports(qname, ref.get_defs())
            elif issubclass(ref, _tosca.ToscaType):
                self._imports[ref.tosca_type_name()] = (qname, ref)

    def _set_ext_imports(self):
        # unfurl's builtin types' import specifier matches tosca name
        # so add those as imports here so we don't try convert the full tosca name to python identifiers
        try:
            from unfurl.tosca_plugins import tosca_ext

            self._add_imports("unfurl", tosca_ext.__dict__)
        except ImportError:
            pass

    def prelude(self) -> str:
        return (
            textwrap.dedent(
                """
        import unfurl
        from typing import List, Dict, Any, Tuple, Union, Sequence
        from typing_extensions import Annotated
        from tosca import (
        Size,
        Time,
        Frequency,
        Bitrate,
        Namespace,
        tosca_version,
        tosca_timestamp,
        operation,
        Property,
        Attribute,
        Requirement,
        Capability,
        Ref,
        InterfaceType,
        CapabilityType,
        NodeType,
        RelationshipType,
        DataType,
        ArtifactType,
        GroupType,
        PolicyType,
        )
        from tosca import *
        import tosca
        """
            )
            + "\n".join([f"import {name}" for name in self._import_statements])
            + "\n"
        )

    def add_import(self, module: str):
        self._import_statements.add(module)

    def get_type_ref(
        self, tosca_type_name: str
    ) -> Tuple[str, Optional[Type[_tosca.ToscaType]]]:
        qname, ref = self._imports.get(tosca_type_name, ("", None))
        if qname:
            # just support one level of import for now
            self._import_statements.add(qname.split(".")[0])
        return qname, ref


class Convert:
    def __init__(
        self,
        template: ToscaTemplate,
        forward_refs=False,
        python_compatible=None,
        builtin_prefix="",
        imports: Optional[Imports] = None,
        custom_defs: Optional[Dict[str, Any]] = None,
        path: Optional[str] = None,
    ):
        self.template = template
        self.names: Dict[str, Any] = {}
        self.topology = template.tpl.get("topology_template") or dict(
            node_templates={}, relationship_templates={}
        )
        self.forward_refs = forward_refs
        if python_compatible is None:
            python_compatible = sys.version_info[1]
        self.python_compatible = python_compatible
        self._builtin_prefix = builtin_prefix
        self.imports = imports or Imports()
        self.import_prefixes: Dict[str, str] = {}
        assert self.template.topology_template
        self.custom_defs = custom_defs or self.template.topology_template.custom_defs
        self.repository_paths: Dict[str, str] = {}
        self.path = path

    def init_names(self, names):
        self.names = names or {}

    def find_repository(self, name) -> Tuple[str, str]:
        if name in ["self"]:
            return name, ""
        name, tosca_name = self._get_name(name)
        if name in self.repository_paths:
            return "tosca_repositories." + name, self.repository_paths[name]
        assert self.template and self.template.tpl
        tpl = self.template.tpl["repositories"][tosca_name or name]
        url = normalize_path(tpl["url"])
        if is_url(url):
            if self.template.import_resolver and self.template.import_resolver.manifest:
                local_path = self.template.import_resolver.manifest.localEnv.link_repo(
                    self.template.path, name, url, tpl.get("revision")
                )
                self.repository_paths[name] = local_path
                return "tosca_repositories." + name, local_path
        else:
            return name, url
        return name, name

    def convert_import(self, tpl) -> Tuple[str, str]:
        repository = tpl.get("repository")
        in_package = False  # XXX
        import_path = os.path.dirname(self.template.path or "")
        if repository:
            module_name, import_path = self.find_repository(repository)
        else:
            module_name = "." if in_package else ""

        dirname, filename = os.path.split(tpl.get("file"))
        # strip out file extension if present
        before, sep, remainder = filename.rpartition(".")
        file_name, tosca_name = self._get_name(before or remainder)
        if dirname:
            modpath = dirname.strip("/").replace("/", ".")
            if module_name and module_name[-1] != ".":
                module_name += "." + modpath
            else:
                module_name += modpath

        import_path = os.path.join(import_path, dirname, file_name)

        uri_prefix = tpl.get("uri_prefix")
        if uri_prefix:
            uri_prefix, tosca_name = self._get_name(uri_prefix)
            tosca_prefix = tosca_name or uri_prefix
            self.import_prefixes[tosca_prefix] = uri_prefix
        if module_name:
            if uri_prefix:
                import_stmt = f"from {module_name} import {file_name} as {uri_prefix}"
            else:
                import_stmt = f"from {module_name}.{file_name} import *"
        else:
            if uri_prefix:
                import_stmt = f"import {file_name} as {uri_prefix}"
            else:
                import_stmt = f"from {file_name} import *"
        return import_stmt + "\n", import_path

    def convert_types(
        self, node_types: dict, section: str, namespace_prefix="", indent=""
    ) -> str:
        src = ""
        self.namespace_prefix = namespace_prefix
        for name in node_types:
            if self._builtin_prefix and not name.startswith(self._builtin_prefix):
                continue
            logging.debug("converting type %s to python", name)
            try:
                toscatype = _make_typedef(name, self.custom_defs, True)
                if toscatype:
                    baseclass_name = section2typename(section)
                    src += (
                        self.nodetype2class(toscatype, baseclass_name, indent) + "\n\n"
                    )
                else:
                    logging.info("couldn't create type %s", name)
            except Exception:
                logging.error("error converting type %s to python", name, exc_info=True)
        return src

    def _builtin_name(self, fullname: str, prefix: str, minimize=False) -> str:
        parts = fullname.split(".")
        if self._builtin_prefix == prefix:
            if minimize and fullname.startswith(self.namespace_prefix):
                prefix = ""
            else:
                # generating for builtins
                # we're in the tosca module so skip the tosca part
                prefix = parts[1] + "."
        else:
            prefix = f"{prefix}{parts[1]}."
        # combine the rest into one identifier
        # (don't capitalize() because it lowercases the rest of the string)
        return prefix + "".join([w[0].upper() + w[1:] for w in parts[2:]])

    def _get_name(self, fullname: str, minimize=False) -> Tuple[str, str]:
        if fullname.startswith("tosca."):
            return self._builtin_name(fullname, "tosca.", minimize), fullname
        elif self._builtin_prefix and fullname.startswith(self._builtin_prefix):
            return (
                self._builtin_name(fullname, self._builtin_prefix, minimize),
                fullname,
            )
        elif (
            minimize
            and self.namespace_prefix
            and fullname.startswith(self.namespace_prefix)
        ):
            name = fullname[len(self.namespace_prefix) :]
        else:
            name = fullname
        toscaname = ""
        if not name.isidentifier():
            toscaname = fullname
            name = re.sub(r"\W", "_", name)
        elif iskeyword(name):
            toscaname = fullname
            name += "_"
        return name, toscaname

    def _set_name(self, fullname: str, fieldtype: str) -> Tuple[str, str]:
        name, toscaname = self._get_name(fullname)
        if name in self.names:
            # if there already is a name clash or if there is about to be one
            if len(self.names[name][1]) > 1 or fieldtype not in self.names[name][1]:
                toscaname = fullname
                # rename on conflict:
                name = name + "_" + fieldtype
        self.names[name] = (fullname, [fieldtype])
        return name, toscaname

    def python_name_from_type(self, tosca_type: str, minimize=False) -> str:
        # we assume the tosca_type has already been imported or is declared in this file
        qname, cls = self.imports.get_type_ref(tosca_type)
        if qname:
            return qname
        if "." in tosca_type:
            parts = tosca_type.split(".")
            if parts[0] in self.import_prefixes:
                # only convert the parts after the prefix
                remainder = ".".join(parts[1:])
                return (
                    self.import_prefixes[parts[0]]
                    + "."
                    + self._get_name(remainder, minimize)[0]
                )
        return self._get_name(tosca_type, minimize)[0]

    def import_types(self, types: List[str]):
        return self.maybe_forward_refs(*(self.python_name_from_type(t) for t in types))

    def maybe_forward_refs(self, *types) -> Sequence[str]:
        if self.forward_refs:
            return [repr(t) for t in types]
        else:
            return types

    def _make_union(self, *types) -> str:
        if self.python_compatible < 10:
            return f"Union[{', '.join(types)}]"
        else:
            # avoid "union syntax can't be used with string operand" error by combining strings
            return (
                " | ".join(types)
                .replace('" | None', ' | None"')
                .replace("' | None", " | None'")
                .replace('" | "', " | ")
                .replace("' | '", " | ")
            )

    def _get_prop_value_repr(self, schema: Schema, value: Any) -> str:
        return self._get_typed_value_repr(schema.type, schema.entry_schema, value)

    def _get_typed_value_repr(self, datatype: str, entry_schema, value: Any) -> str:
        if value is None:
            return "None"
        if has_function(value):
            return value2python_repr(value)
        typename = _tosca.TOSCA_SIMPLE_TYPES.get(datatype)
        if entry_schema:
            entry_schema = Schema(None, entry_schema)
            if typename == "Dict":
                items = [
                    f"{k}: {self._get_prop_value_repr(entry_schema, item)}"
                    for k, item in value.items()
                ]
                return "{" + ", ".join(items) + "}"
            else:
                assert typename == "List"
                items = [
                    self._get_prop_value_repr(entry_schema, item) for item in value
                ]
                return "[" + ", ".join(items) + "]"
        else:
            if datatype.startswith("scalar-unit."):
                if isinstance(value, (list, tuple)):
                    # for in_range constraints
                    return self._get_typed_value_repr(
                        "list", dict(type=datatype), value
                    )
                scalar_unit_class = get_scalarunit_class(datatype)
                assert scalar_unit_class
                canonical = scalar_unit_class(value).validate_scalar_unit()
                # XXX add unit to imports
                return canonical.strip().replace(" ", "*")  # value * unit
            if typename:
                # simple value type
                return value2python_repr(value)
            else:
                # its a tosca datatype
                typename, cls = self.imports.get_type_ref(datatype)
                if typename:
                    assert cls and issubclass(cls, _tosca.DataType)
                    dt = cls.get_tosca_datatype()
                else:
                    # hasn't been imported yet, must be declared in this file
                    typename, toscaname = self._get_name(datatype)
                    # use a TOSCA datatype
                    dt = DataType(datatype, self.custom_defs)
                    cls = None
                if dt.value_type:
                    # its a simple value type
                    return value2python_repr(value)
                if not isinstance(value, dict):
                    logging.error(
                        "expected a dict value for %s, got: %s", datatype, value
                    )
                    return str(value)
                return self.convert_datatype_value(typename, cls, dt, value)

    def to_constraints(self, constraints):
        # note: c.constraint_value_msg is unconverted value
        # constraint_key will correspond to constraint class names
        src_list = [
            f"{c.constraint_key}({self._get_typed_value_repr(c.property_type, None, c.constraint_value_msg)})"
            for c in constraints
        ]
        if len(src_list) == 1:
            return f"({src_list[0]},)"
        return f"({', '.join(src_list)})"

    def _prop_type(self, schema: Schema) -> str:
        datatype = schema.type
        typename = _tosca.TOSCA_SIMPLE_TYPES.get(datatype)
        if not typename:
            # it's a tosca datatype
            datatype = _tosca.TOSCA_SHORT_NAMES.get(datatype, datatype)
            typename = self.python_name_from_type(datatype)
            if self.forward_refs:
                typename = repr(typename)
        if schema.entry_schema:
            item_type_name = self._prop_type(Schema(None, schema.entry_schema))
            if typename == "Dict":
                typename += f"[str, {item_type_name}]"
            else:
                assert typename == "List"
                typename += f"[{item_type_name}]"

        if schema.constraints:
            typename = (
                f"Annotated[{typename}, {self.to_constraints(schema.constraints)}]"
            )
        return typename

    def _prop_decl(
        self, propdef: PropertyDef, fieldtype: str, both: bool
    ) -> Tuple[str, str, str]:
        name, toscaname = self._set_name(propdef.name, "property")
        fieldparams = []
        if toscaname:
            fieldparams.append(f'name="{toscaname}"')
        assert isinstance(propdef.schema, dict)
        prop = Property(propdef.name, None, propdef.schema, self.custom_defs)
        typename = self._prop_type(prop.schema)
        if not propdef.required:
            typename = self._make_union(typename, "None")
        default_value: Any = MISSING
        if "default" in propdef.schema:
            default_value = propdef.schema["default"]
        elif not propdef.required:
            default_value = None

        if prop.schema.title:
            fieldparams.append(f"title={value2python_repr(prop.schema.title, True)}")
        if prop.schema.status:
            fieldparams.append(f"status={value2python_repr(prop.schema.status, True)}")
        if prop.schema.metadata:
            fieldparams.append(f"metadata={metadata_repr(prop.schema.metadata)}")
        if both:
            fieldparams.append(f"attribute=True")

        if default_value is not MISSING:
            value_repr = self._get_prop_value_repr(prop.schema, default_value)
            if value_repr[0] in ("{", "["):
                fieldparams.append(f"default_factory=lambda:({value_repr})")
            elif fieldparams:
                fieldparams.append(f"default={value_repr}")
            else:
                fielddecl = f"= {value_repr}"
        else:
            fielddecl = ""

        if fieldtype == "attributes":
            fielddecl = f"= Attribute({', '.join(fieldparams)})"
        elif fieldparams:
            fielddecl = f"= Property({', '.join(fieldparams)})"
        return name, typename, fielddecl  # type: ignore

    def _get_baseclass_names(
        self, entity_type: StatefulEntityType, baseclass_name: str
    ) -> str:
        parents = entity_type.parent_types()
        if not parents:
            base_names = baseclass_name
        else:
            base_names = ", ".join(
                [self.python_name_from_type(p.type, True) for p in parents]
            )
        interfaces = entity_type.get_value(entity_type.INTERFACES) or {}
        for name, val in interfaces.items():
            itype = val and val.get("type")
            if itype:
                # don't add interface to bases if a base type already declared it
                for p in parents:
                    if p is entity_type:
                        continue
                    parent_interfaces = p.get_value(entity_type.INTERFACES)
                    if parent_interfaces and name in parent_interfaces:
                        break
                else:
                    base_names += ", " + self.python_name_from_type(itype, True)
        return base_names

    def _get_type_names(
        self, current_type: StatefulEntityType
    ) -> Dict[str, Tuple[str, List[str]]]:
        # keys: requirement, capability, operation, property (includes attributes)
        names: Dict[str, Tuple[str, List[str]]] = {}
        entity_type = current_type.parent_type
        if not entity_type:
            return names
        props = entity_type.get_definition("properties") or {}
        for name in props:
            names[name] = (name, ["property"])
        attrs = entity_type.get_definition("attributes") or {}
        for name in attrs:
            names[name] = (name, ["property"])
        if isinstance(entity_type, NodeType):
            reqs = entity_type.requirements or []
            for req in reqs:
                name = list(req)[0]
                names.setdefault(name, (name, []))[1].append("requirement")
            capabilities = entity_type.get_capabilities_def()
            for name in capabilities:
                names.setdefault(name, (name, []))[1].append("capability")
        for iname, idef in entity_type.interfaces.items():
            ops = idef.get("operations") or {}
            for name in ops:
                names.setdefault(name, (name, []))[1].append("operation")
        return names

    def nodetype2class(
        self, nodetype: StatefulEntityType, baseclass_name: str, initial_indent=""
    ) -> str:
        indent = "    "
        self.init_names(self._get_type_names(nodetype))
        # XXX list of imports
        toscaname = nodetype.type
        cls_name = self.python_name_from_type(toscaname, True)
        base_names = self._get_baseclass_names(nodetype, baseclass_name)

        class_decl = f"{initial_indent}class {cls_name}({base_names}):\n"
        src = ""
        indent = initial_indent + indent
        # XXX: 'version', 'artifacts'
        assert nodetype.defs
        src += add_description(nodetype.defs, indent)
        if toscaname != cls_name:
            src += f'{indent}_tosca_name = "{toscaname}"\n'
        metadata = nodetype.defs.get("metadata")
        if metadata:
            formatted = textwrap.indent(
                metadata_repr(metadata),
                indent + indent,
            )
            src += f"{indent}_type_metadata = {formatted}\n"

        src += self.add_properties_decl(nodetype, "properties", indent)
        src += self.add_properties_decl(nodetype, "attributes", indent)

        caps = nodetype.get_value("capabilities")
        if caps:
            for name, tpl in caps.items():
                src += self.add_capability(name, tpl, indent)

        if isinstance(nodetype, NodeType):
            reqs = nodetype.requirement_definitions
            for tpl in nodetype.get_value("requirements") or []:
                assert tpl, tpl
                req_name, req = list(tpl.items())[0]
                # get the full req including inherited values
                src += self.add_req(req_name, reqs[req_name], indent)

        if baseclass_name == "InterfaceType":
            # inputs and operations are defined directly on the body of the type
            for op in _create_operations({toscaname: nodetype.defs}, nodetype, None):
                src += self.operation2func(op, indent, []) + "\n"
        else:
            # 3.7.5.2 operation definitions.
            src += self._add_operations(nodetype, indent)

        target_types = nodetype.get_value("valid_target_types")
        if target_types:
            src += f'{indent}_valid_target_types = [{", ".join(self.import_types(target_types))}]\n'

        # artifact, relationship and datatype special keys
        for key in ["file_ext", "mime_type", "type", "constraints", "default_for"]:
            value = nodetype.get_value(key)
            if value:
                src += f"{indent}_{key} = {value2python_repr(value, True)}\n"

        if src.strip():
            return class_decl + src
        else:
            return class_decl + f"{indent}pass"

    def _add_operations(self, nodetype: StatefulEntityType, indent: str) -> str:
        # XXX
        # XXX default_ops doesn't seem to be working (see artifacts.yaml.py)
        src = ""
        default_ops = []
        for iname, interface in nodetype.interfaces.items():
            if "operations" in interface:
                ops = interface["operations"]
            else:
                ops = interface
            for oname, op in ops.items():
                if not op:
                    default_ops.append((iname, oname))

        defaulted = False
        declared_ops = []
        declared_interfaces: Optional[Dict] = nodetype.get_value("interfaces")
        declared_requirements = []
        if declared_interfaces:
            for iname, interface in declared_interfaces.items():
                requirements = interface.get("requirements")
                if requirements:
                    declared_requirements.extend(requirements)
                if "operations" in interface:
                    ops = interface["operations"]
                else:
                    ops = interface
                for oname, op in ops.items():
                    if op:
                        declared_ops.append((iname, oname))

        if declared_requirements:
            # include inherited interface_requirements
            src += f"{indent}_interface_requirements = {value2python_repr(nodetype.get_interface_requirements())}\n"

        for op in EntityTemplate._create_interfaces(nodetype, None):
            if op.interfacetype != "Mock":
                op_id = (op.interfacename, op.name)
                if op_id in declared_ops and op_id not in default_ops:
                    src += self.operation2func(op, indent, default_ops) + "\n"
                elif op.name == "default" and not defaulted:
                    defaulted = True  # only generate once
                    src += self.operation2func(op, indent, default_ops) + "\n"
        return src

    def add_properties_decl(
        self, nodetype: StatefulEntityType, fieldname: str, indent: str
    ) -> str:
        src = ""
        declared_props = nodetype.get_value(fieldname)
        if not declared_props:
            return ""
        props = nodetype.get_definition(fieldname)
        if fieldname == "attributes":
            shadowed = nodetype.get_value("properties") or {}
        else:
            shadowed = nodetype.get_value("attributes") or {}
        for name, schema in props.items():
            both = False
            if name not in declared_props:
                # exclude inherited properties
                continue
            if name in shadowed:
                if fieldname == "attributes":
                    continue
                else:
                    both = True
            prop = PropertyDef(name, None, schema)
            name, typedecl, fielddecl = self._prop_decl(prop, fieldname, both)
            src += f"{indent}{name}: {typedecl} {fielddecl}\n"
            src += add_description(prop.schema, indent)
        if src:
            src += "\n"
        return src

    def _set_arity(self, typedecl, _min, _max, default: str) -> Tuple[str, str]:
        if _max == "UNBOUNDED" or _max > 1:
            typedecl = f"Sequence[{typedecl}]"  # use sequence for covariance
            if default:
                default = f"=({default},)"
            elif _min == 0:
                default = "=()"
        elif _min == 0:
            typedecl = self._make_union(typedecl, "None")
            default = "=None"
        return typedecl, default

    def add_capability(self, name, tpl, indent) -> str:
        # Capability(factory=typename) (if no required properties) or default=None or ()
        fieldparams = []
        name, toscaname = self._set_name(name, "capability")
        if toscaname:
            fieldparams.append(f'name="{toscaname}"')
        cap_type_name = self.python_name_from_type(tpl["type"])
        typedecl = self.maybe_forward_refs(cap_type_name)[0]
        default = ""
        if "occurrences" in tpl:
            min, max = tpl["occurrences"]
            typedecl, default = self._set_arity(typedecl, min, max, default)
        if default:
            fieldparams.append("default" + default)
        else:
            # XXX only set this if capability doesn't have any required properties
            if typedecl.startswith("Sequence"):
                factory = f"lambda: [{cap_type_name}()]"
            else:
                factory = cap_type_name
            fieldparams.append("factory=" + factory)
        valid_source_types = tpl.get("valid_source_types")
        if valid_source_types:
            fieldparams.append(
                f"valid_source_types={value2python_repr(valid_source_types, True)}"
            )
        metadata = tpl.get("metadata")
        if metadata:
            fieldparams.append(f"metadata={metadata_repr(metadata)}")
        if fieldparams:
            fielddecl = f"= Capability({', '.join(fieldparams)})\n"
        else:
            fielddecl = ""
        src = f"{indent}{name}: {typedecl} {fielddecl}\n"
        src += add_description(tpl, indent)
        return src

    def _get_req_types(self, req: dict) -> Tuple[List[str], str, bool]:
        types: List[str] = []
        relationship = req.get("relationship")
        if relationship:
            if isinstance(relationship, dict):
                relationship = relationship["type"]
            elif relationship in self.topology.get("relationship_templates", {}):
                reltpl = cast(
                    dict, self.topology["relationship_templates"][relationship]
                )
                relationship = reltpl["type"]
            if relationship != "tosca.relationships.Root":
                types.append(relationship)

        match = ""
        nodetype = req.get("node")
        if nodetype:
            # req['node'] can be a node_template instead of a type
            if nodetype in self.topology["node_templates"]:
                entity_tpl = cast(dict, self.topology["node_templates"][nodetype])
                match = nodetype
                nodetype = entity_tpl["type"]
            types.append(expand_prefix(nodetype))

        cap = req.get("capability")
        if cap:
            # if no other types set flag to add requirement() to distinguish this from a capability
            explicit = not bool(types)
            types.append(cap)
        else:
            explicit = False
        return types, match, explicit

    def add_req(self, req_name: str, req: dict, indent: str) -> str:
        if isinstance(req, str):
            req = dict(node=req)
        types, match, explicit = self._get_req_types(req)
        # XXX add rel.valid_target_types
        types = self.import_types(types)
        if not types:
            # XXX need to merge with base requirements
            logging.error("req missing types %s", req)
            return ""
        if len(types) > 1:
            typedecl = self._make_union(*types)
        else:
            typedecl = types[0]

        if match:
            # XXX because a node can't be created before its type definition, match needs to be a Ref()
            match = f'="{match}"'
        if "occurrences" in req:
            min, max = req["occurrences"]
            typedecl, default = self._set_arity(typedecl, min, max, match)
        else:
            default = match

        fieldparams = []
        name, toscaname = self._set_name(req_name, "requirement")
        if toscaname:
            fieldparams.append(f'name="{toscaname}"')
        node_filter = req.get("node_filter")
        if node_filter:
            fieldparams.append(f"node_filter={value2python_repr(node_filter, True)}")
        metadata = req.get("metadata")
        if metadata:
            fieldparams.append(f"metadata={metadata_repr(metadata)}")
        if fieldparams or explicit:
            if default:
                fieldparams.insert(0, "default" + default)
            fielddecl = f"= Requirement({', '.join(fieldparams)})"
        else:
            fielddecl = default
        src = f"{indent}{name}: {typedecl} {fielddecl}\n"
        src += add_description(req, indent)
        return src

    def get_configurator_decl(self, op: OperationDef) -> Tuple[str, Dict[str, Any]]:
        kw = (
            self.template.import_resolver.find_implementation(op)
            if self.template.import_resolver
            else None
        )
        cmd = ""
        if kw is None:
            if isinstance(op.implementation, dict):
                artifact = op.implementation["primary"]
                kw = op.implementation.copy()
            else:
                artifact = op.implementation
                kw = dict(primary=artifact)
            kw["inputs"] = op.inputs
            if isinstance(artifact, str):
                artifact, toscaname = self._get_name(artifact)
                if hasattr(self, artifact):
                    # add direct reference to allow static type checking
                    cmd = f"self.{artifact}.execute("
            if not cmd and artifact:
                cmd = f"self.find_artifact({value2python_repr(artifact)})"
        else:
            cmd = kw["className"]
            module, sep, klass = cmd.rpartition(".")
            if module:
                if module.endswith(
                    "_py"
                ):  # hack for now... see load_module in unfurl.util
                    module_path = sys.modules[module].__file__
                    assert module_path
                    if self.path:
                        module_path = os.path.relpath(
                            module_path, start=os.path.abspath(self.path)
                        )
                    cmd = f'self.load_class("{module_path}", "{klass}")'
                else:
                    self.imports.add_import(module)

        # XXX other kw:
        # environment, dependencies, operation_host, timeout
        return cmd, kw

    # XXX if default operation defined with empty operations, don't call operation2func, add decorator instead
    # XXX track python imports
    def operation2func(
        self, op: OperationDef, indent: str, default_ops: List[Tuple[str, str]]
    ) -> str:
        # XXX op.invoke
        # iDef.entry_state: add to decorator
        # note: defaults and base class inputs and implementations already merged in
        # artifact property reference or configurator class
        configurator_decl, kw = self.get_configurator_decl(op)
        name, toscaname = self._set_name(op.name, "operation")
        src = ""
        decorator = []
        if toscaname:
            decorator.append(f'name="{toscaname}"')
        if op.name == "default":
            apply_to = ", ".join([f'"{op[0]}.{op[1]}"' for op in default_ops])
            decorator.append(f"apply_to=[{apply_to}]")
        if decorator:  # add decorator
            src += f"{indent}@operation({', '.join(decorator)})\n"

        # XXX add arguments declared on the interface definition
        # XXX declare configurator/artifact as the return value
        src += f"{indent}def {name}(self):\n"
        indent += "   "
        desc = add_description(op.value, indent)
        src += desc
        if not configurator_decl:
            # XXX implement not_implemented, treat this as not_implemented
            if not desc:
                src += f"{indent}pass\n"
            return src
        src += f"{indent}return {configurator_decl}("
        # all on one line for now
        inputs = kw["inputs"]
        if inputs:
            src += "\n"
            for name, value in inputs.items():
                # use encode_identifier to handle input names that aren't valid python identifiers
                src += f"{indent}{indent}{encode_identifier(name)} = {value2python_repr(value)},"
            src += f"{indent})\n"
        else:
            src += ")\n"
        return src

    def convert_datatype_value(
        self,
        classname: str,
        cls: Optional[Type[_tosca.ToscaType]],
        dt: DataType,
        value: Dict[str, Any],
        indent="",
    ) -> str:
        # convert dict to the datatype
        src = f"{indent}{classname}("
        props = dt.get_properties_def()
        for key, val in value.items():
            prop = props.get(key)
            if prop:
                if not isinstance(prop.schema, Schema):
                    schema = Schema(key, prop.schema)
                else:
                    schema = prop.schema
                prop_repr = self._get_prop_value_repr(schema, val)
            else:
                prop_repr = value2python_repr(val)
            field_name = cls and cls.tosca_names.get(key) or ""
            if not field_name:
                # XXX names should be from the type namespace
                # XXX add _extra field and set that
                field_name, tosca_name = self._set_name(key, "extra")
            src += f"{field_name}={prop_repr},\n"
        src += ")"
        return src

    def node_template2obj(self, node_template, indent="") -> str:
        self.init_names({})
        cls_name, cls = self.imports.get_type_ref(node_template.type)
        assert cls
        # XXX names should be from parent namespace (module or Namespace)
        name, toscaname = self._set_name(node_template.name, "template")
        src = f"{indent}{name} = {cls_name}("
        # always add name because we might not have access to the name reference
        src += f'_name="{toscaname or name}", '
        if node_template.metadata:
            src += f"_metadata={metadata_repr(node_template.metadata)},\n"
        # XXX version
        if node_template.directives:
            src += f'_directives=[{", ".join(repr(node_template.directives))}],\n'

        for prop in node_template.get_property_objects():
            field = cls.tosca_names.get(prop.name)
            if field:
                field_name = field.name
            else:
                # XXX names should be from the type namespace
                # XXX add _extra field and add this to that
                field_name, tosca_name = self._set_name(prop.name, "extra")
            src += (
                f"{field_name}={self._get_prop_value_repr(prop.schema, prop.value)},\n"
            )
        # note: the toscaparser doesn't support declared attributes currently
        src += ")"  # close ctor
        # add these as attribute statements
        # XXX capabilities
        # XXX requirements
        # XXX operations: declare than assign
        if node_template.description and node_template.description.strip():
            src += f"{indent}{name}._description = " + add_description(
                node_template.description, indent
            )
        return src

    def follow_import(self, import_def, import_path, format):
        # we've already imported this, get its contents
        file_path = str(Path(import_path).parent / Path(import_def["file"]).name)
        if file_path not in self.template.nested_tosca_tpls:
            logging.warning(
                f"can't import: {file_path} not found in {list(self.template.nested_tosca_tpls)}"
            )
            return
        tpl = self.template.nested_tosca_tpls[file_path]
        tpl["tosca_definitions_version"] = self.template.tpl[
            "tosca_definitions_version"
        ]
        if "repositories" in self.template.tpl:
            repositories = self.template.tpl["repositories"]
            tpl.setdefault("repositories", {}).update(repositories)
        convert_service_template(
            ToscaTemplate(
                file_path,
                yaml_dict_tpl=tpl,
                import_resolver=self.template.import_resolver,
                verify=False,
            ),
            self.python_compatible,
            self._builtin_prefix,
            format,
            custom_defs=self.custom_defs,
            path=import_path,
        )


def generate_builtins(import_resolver, format=True) -> str:
    custom_defs = EntityType.TOSCA_DEF.copy()
    custom_defs["tosca.nodes.Root"] = custom_defs["tosca.nodes.Root"].copy()
    # remove the Install interface, we need to manually add it later
    custom_defs["tosca.nodes.Root"]["interfaces"] = {
        "Standard": {"type": "tosca.interfaces.node.lifecycle.Standard"}
    }

    return convert_service_template(
        ToscaTemplate(
            path=EntityType.TOSCA_DEF_FILE,
            yaml_dict_tpl=EntityType.TOSCA_DEF_LOAD_AS_IS,
            import_resolver=import_resolver,
        ),
        7,
        f"tosca.",
        format,
        custom_defs,
    )


def generate_builtin_extensions(import_resolver) -> str:
    def_path = ToscaTemplate.exttools.get_defs_file("tosca_simple_unfurl_1_0_0")
    return convert_service_template(
        ToscaTemplate(path=def_path, import_resolver=import_resolver),
        7,
        f"unfurl.",
        True,
        EntityType.TOSCA_DEF,
    )


def convert_service_template(
    template: ToscaTemplate,
    python_compatible=None,
    builtin_prefix="",
    format=True,
    custom_defs=None,
    path="",
) -> str:
    src = ""
    imports = Imports()
    if not builtin_prefix:
        imports._set_ext_imports()
    tpl = cast(Dict[str, Any], template.tpl)
    converter = Convert(
        template, True, python_compatible, builtin_prefix, imports, custom_defs, path
    )
    imports_tpl = tpl.get("imports")
    if imports_tpl and isinstance(imports_tpl, list):
        for imp_def in imports_tpl:
            if isinstance(imp_def, str):
                imp_def = dict(file=imp_def)
            import_src, import_path = converter.convert_import(imp_def)
            converter.follow_import(imp_def, import_path + ".py", format)
            src += import_src

    # interface_types needs to go first because they will be base classes for types that implement them
    # data_types and capability_types can be set as defaults so they also need to be defined early
    # see EntityType.TOSCA_DEF_SECTIONS for list
    sections = [
        "interface_types",
        "data_types",
        "artifact_types",
        "capability_types",
        "relationship_types",
        "node_types",
        "group_types",
        "policy_types",
    ]
    for section in sections:
        type_tpls = tpl.get(section)
        if type_tpls:
            if builtin_prefix:
                # e.g. node_types -> nodes
                tosca_type = dict(
                    data_types="datatypes",
                    capability_types="capabilities",
                    policy_types="policies",
                ).get(section, section[: -len("_types")] + "s")
                ns_prefix = f"{builtin_prefix}{tosca_type}."
            else:
                ns_prefix = ""
                tosca_type = ""
            indent = "   " if ns_prefix else ""
            class_src = converter.convert_types(type_tpls, section, ns_prefix, indent)
            if class_src:
                if ns_prefix:
                    src += f"class {tosca_type}(Namespace):\n" + class_src
                else:
                    src += class_src + "\n"
    prologue = f"# Generated by tosca.yaml2python from {os.path.relpath(template.path)} at {datetime.datetime.now()}\n"
    src = prologue + imports.prelude() + src
    src += '\nif __name__ == "__main__":\n    tosca.dump_yaml(globals())'

    if format:
        try:
            src = black.format_file_contents(src, fast=True, mode=black.mode.Mode())
        except black.report.NothingChanged:
            pass
        except Exception as e:
            logging.error("failed to format %s: %s", path, e)
    if path:
        with open(path, "w") as po:
            print(src, file=po)
    return src
