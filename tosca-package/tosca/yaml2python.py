# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Converts a TOSCA service template from YAML to Python.

Repositories are resolved with by creating a`tosca_repositories` directory with symlinks to the source.
Imports to followed and converted in-place.

Usage:

unfurl export --format python ensemble-template.yaml
"""

from dataclasses import MISSING
import importlib
import logging
import os
from pathlib import Path, PurePath
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
    Mapping,  # use mapping to when there are dicts we don't want to accidentally modify
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from toscaparser.artifacts import Artifact
from toscaparser.imports import is_url, normalize_path
from toscaparser.elements.nodetype import NodeType
from toscaparser.elements.relationshiptype import RelationshipType
from toscaparser.elements.statefulentitytype import StatefulEntityType
from toscaparser.elements.property_definition import PropertyDef
from toscaparser.entity_template import EntityTemplate
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.relationship_template import RelationshipTemplate
from toscaparser.properties import Property
from toscaparser.elements.interfaces import OperationDef, create_operations
from toscaparser.tosca_template import ToscaTemplate
from toscaparser.topology_template import TopologyTemplate, find_type
from toscaparser.elements.entity_type import EntityType, Namespace
from toscaparser.elements.datatype import DataType
from toscaparser.elements.artifacttype import ArtifactTypeDef
from toscaparser.elements.constraints import constraint_mapping, Schema
from toscaparser.elements.scalarunit import get_scalarunit_class
from keyword import iskeyword
from . import WritePolicy, _tosca, ToscaFieldType, has_function, loader, __all__
import black
import black.mode
import black.report

try:
    import unfurl
except ImportError:
    unfurl = None  # type: ignore  # not installed

logger = logging.getLogger("tosca")


value_indent = 2


def _pprint_multiline(self, object, stream, indent, allowance, context, level):
    lines = str(object)
    if "\n" not in lines:
        return pprint.PrettyPrinter._pprint_str(  # type:ignore
            self, object, stream, indent, allowance, context, level
        )  # type: ignore
    # we dont want to indent since that might affect correctness if leading spaces in the string are significant
    stream.write(multiline_repr(lines, "", "r", "\\"))


try:
    from ruamel.yaml.comments import CommentedMap
    from ruamel.yaml.scalarstring import LiteralScalarString, FoldedScalarString

    def __repr__(self):
        return dict.__repr__(self)

    CommentedMap.__repr__ = __repr__  # type: ignore
    pprint.PrettyPrinter._pprint_ordered_dict = pprint.PrettyPrinter._pprint_dict  # type: ignore
    pprint.PrettyPrinter._dispatch[  # type: ignore
        CommentedMap.__repr__
    ] = pprint.PrettyPrinter._pprint_dict  # type: ignore

    pprint.PrettyPrinter._dispatch[  # type: ignore
        LiteralScalarString.__repr__
    ] = _pprint_multiline  # type: ignore
    pprint.PrettyPrinter._dispatch[  # type: ignore
        FoldedScalarString.__repr__
    ] = _pprint_multiline  # type: ignore
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


def value2python_repr(value, quote=False, imports: Optional["Imports"] = None) -> str:
    if sys.version_info.minor > 7:
        pprinted = pprint.pformat(
            value, compact=True, indent=value_indent, sort_dicts=False
        )  # type: ignore  # for py3.7 mypy
    else:
        pprinted = pprint.pformat(value, compact=True, indent=value_indent)
    if not quote:
        if has_function(value):
            if imports:
                imports.add_tosca_from("Eval")
            return f"Eval({pprinted})"
    return pprinted


def multiline_repr(description, indent, prefix="", suffix="") -> str:
    for q in ['"""', "'''", '"', '"']:
        # avoid """foo""""
        if q not in description and description[-1] != q[0]:
            quote = prefix + q + suffix
            break
    else:
        return indent + repr(description)
    if "\n" in description:
        src = f"{indent}{quote}\n"
        src += textwrap.indent(description.rstrip(), indent)
        src += f"\n{indent}{q}\n\n"
    else:
        src = f"{indent}{quote}{description}{q}\n\n"
    return src


def add_description(defs, indent) -> str:
    description = isinstance(defs, dict) and defs.get("description")
    if description:
        return multiline_repr(description, indent)
    else:
        return ""


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
    name, sep, suffix = section.partition("_")
    if name in ("capability", "artifact", "data"):
        return string.capwords(name) + "Entity"
    else:
        return string.capwords(name)


Scope = Dict[
    str, Tuple[str, Union[None, Type[_tosca.ToscaType], Type[_tosca.ValueType]]]
]


class Imports:
    def __init__(self, unfurl_prelude: bool):
        self._imports: Scope = {}
        self.prelude_prelude: str = "import unfurl" if unfurl_prelude else ""
        self._import_statements: Set[str] = set()
        self.declared: List[str] = []  # XXX should have names from prelude_prelude
        self._all: List[str] = []
        self.from_tosca: Set[str] = set()
        self._globals: Dict[str, Any] = {}

    def add_tosca_from(self, name):
        self.from_tosca.add(name)
        return name

    def get_all(self):
        return self._all

    def add_declaration(self, tosca_name: str, localname: str, include_in_all=True):
        # new obj is being declared in the current module in the current scope
        self._imports[tosca_name] = (localname, None)
        self.declared.append(localname)
        if include_in_all:
            self._all.append(localname)

    def enter_scope(self) -> Tuple[List[str], Scope]:
        declared = self.declared
        self.declared = []
        imports = self._imports
        self._imports = imports.copy()
        return declared, imports

    def exit_scope(self, declared: List[str], imports: Scope):
        self._imports = imports
        self.declared = declared

    def _add_imports(self, basename: str, namespace: Mapping[str, Any]):
        for name, ref in namespace.items():
            if basename:
                qname = basename + "." + name
            else:
                qname = name
            if not isinstance(ref, type):
                # XXX handle importing templates
                continue
            elif issubclass(ref, _tosca.Namespace):
                self._add_imports(qname, ref.get_defs())
            elif issubclass(ref, (_tosca.ToscaType, _tosca.ValueType)):
                tosca_name = ref.tosca_type_name()
                current = self._imports.get(tosca_name)
                if not current or current[1] is not ref:
                    self._imports[tosca_name] = (qname, ref)
                    if not basename:
                        self._globals[name] = ref
                # otherwise skip aliases

    def add_imports(self, basename: str, namespace: Mapping[str, Any]):
        if basename:
            self.declared.append(basename)
        else:  # from X import *
            self.declared.extend(namespace.keys())
        self._add_imports(basename, namespace)

    def _set_builtin_imports(self):
        # unfurl's builtin types' import specifier matches tosca name
        # so add those as imports here so we don't try convert the full tosca name to python identifiers
        try:
            from . import builtin_types

            self.add_imports("tosca", builtin_types.__dict__)
        except ImportError:
            pass

    def _set_ext_imports(self):
        # unfurl's builtin types' import specifier matches tosca name
        # so add those as imports here so we don't try convert the full tosca name to python identifiers
        try:
            from unfurl.tosca_plugins import tosca_ext

            self.add_imports("unfurl", tosca_ext.__dict__)
        except ImportError:
            pass

    def prelude(self) -> str:
        if self.from_tosca:
            from_tosca_stmt = (
                f"from tosca import ({', '.join(sorted(self.from_tosca))})\n"
            )
        else:
            from_tosca_stmt = ""
        return (
            textwrap.dedent(
                f"""
        {self.prelude_prelude}
        from typing import List, Dict, Any, Tuple, Union, Sequence
        import tosca
        """
            )
            + from_tosca_stmt
            + "\n".join([f"import {name}" for name in sorted(self._import_statements)])
            + "\n"
        )

    def add_import(self, module: str):
        self._import_statements.add(module)
        self.declared.append(module)

    def get_local_ref(self, tosca_name) -> str:
        qname, ref = self._imports.get(tosca_name, ("", None))
        return qname

    def get_type_ref(
        self, tosca_type_name: str
    ) -> Tuple[str, Union[None, Type[_tosca.ToscaType], Type[_tosca.ValueType]]]:
        qname, ref = self._imports.get(tosca_type_name, ("", None))
        if ref:
            ref._globals = self._globals
            parts = qname.split(".")
            # qname in namespace, import it
            if len(parts) > 1 and parts[0] not in ["tosca", "unfurl"]:  # in prelude
                # just support one level of import for now
                self.add_import(parts[0])
        return qname, ref


def inline_comment(comment: str) -> str:
    # avoid black moving comments to the wrong line
    return comment + "\n" if comment else ""


class Convert:
    convert_built_in = False

    def __init__(
        self,
        template: ToscaTemplate,
        forward_refs=False,
        python_compatible: Optional[int] = None,
        builtin_prefix="",
        imports: Optional[Imports] = None,
        custom_defs: Optional[Dict[str, Any]] = None,
        path: Optional[str] = None,
        write_policy: WritePolicy = WritePolicy.auto,
        base_dir: Optional[str] = None,
        package_name: str = "service_template",
    ):
        self.template = template
        # local namespace of tosca names
        # the same name can appear in different positions (the value of the dict)
        self.local_names: Dict[str, str] = {}
        self.has_overrides: Optional[List[str]] = []
        self.base_refs: Dict[str, Optional[str]] = {}
        self._pending_defs: List[str] = []
        self.topology = (
            template.tpl and template.tpl.get("topology_template")
        ) or dict(node_templates={}, relationship_templates={})
        self.forward_refs = forward_refs
        if python_compatible is None:
            python_compatible = sys.version_info[1]
        self.python_compatible = python_compatible
        self._builtin_prefix = builtin_prefix
        self.imports = imports or Imports(bool(unfurl))
        self.import_prefixes: Dict[str, str] = {}
        assert self.template.topology_template
        self.custom_defs = custom_defs or self.template.topology_template.custom_defs
        self.repository_paths: Dict[str, str] = {}
        self.path = path
        self.write_policy = write_policy
        assert self.template.path
        self.base_dir = base_dir or os.path.dirname(self.template.path)
        self.package_name = package_name
        self.concise = os.getenv("UNFURL_EXPORT_PYTHON_STYLE") == "concise"
        self.assign_attr = self.concise

    def value2python_repr(self, value, quote=False) -> str:
        return value2python_repr(value, quote, self.imports)

    def is_tosca_type(self, typename: str) -> bool:
        return typename in EntityType.TOSCA_DEF or typename in self.custom_defs

    def convert_topology(self, topology: TopologyTemplate, indent="") -> str:
        src = ""
        if topology.inputs:
            self.imports.from_tosca.add("TopologyInputs")
            src += indent + "class Inputs(TopologyInputs):\n"
            input_indent = indent + "    "
            for input in topology.inputs:
                name, typedecl, fielddecl = self._prop_decl(input, "properties", False)
                src += f"{input_indent}{name}: {typedecl} {fielddecl}\n"
                src += add_description(input.schema, input_indent)
        if topology.outputs:
            self.imports.from_tosca.add("TopologyOutputs")
            src += indent + "class Outputs(TopologyOutputs):\n"
            input_indent = indent + "    "
            for output in topology.outputs:
                name, typedecl, fielddecl = self._prop_decl(output, "properties", False)
                src += f"{input_indent}{name}: {typedecl} {fielddecl}\n"
                src += add_description(output.schema, input_indent)

        root_node = (
            topology.substitution_mappings and topology.substitution_mappings.node
        )
        for node_template in topology.nodetemplates:
            localname = self.imports.get_local_ref(node_template.name)
            if not localname or localname not in self.imports.declared:
                template_name, template_src = self.node_template2obj(
                    node_template, indent
                )
                src += self.flush_pending_defs(indent)
                if template_src:
                    src += template_src + "\n"
        for rel_name, rel_template in topology.relationship_templates.items():
            localname = self.imports.get_local_ref(rel_name)
            if not localname or localname not in self.imports.declared:
                template_name, template_src = self.relationship_template2obj(
                    rel_template, indent
                )
                src += self.flush_pending_defs(indent)
                if template_src:
                    src += template_src + "\n"
        if root_node:
            root_template = self.imports.get_local_ref(root_node)
            src += f"{indent}__root__ = {root_template}\n"
        return src

    def convert_blueprint(self, blueprint: str, tpl) -> str:
        src = f"class {blueprint}(DeploymentBlueprint):\n"
        indent = "   "
        tpl = tpl.copy()  # we need to remove fields for TopologyTemplate()
        for fieldname in _tosca.DeploymentBlueprint._fields:
            key = fieldname[1:]
            if key in tpl:
                src += f"{indent}{fieldname} = {self.value2python_repr(tpl.pop(key))}\n"
        src += "\n"
        assert self.template.topology_template
        topology = TopologyTemplate(
            tpl,
            self.template.topology_template.custom_defs,
            self.template.parsed_params,
            self.template,
        )
        declared, imports = self.imports.enter_scope()
        src += self.convert_topology(topology, indent)
        self.imports.exit_scope(declared, imports)
        return src

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
            if self.template.import_resolver:
                local_path = self.template.import_resolver.find_repository_path(
                    name, tpl, self.template.base_dir
                )
                if local_path:
                    self.repository_paths[name] = local_path
                    return "tosca_repositories." + name, local_path
                else:
                    logger.error(
                        'Bad import: can not find repository "%s" in "%s"',
                        name,
                        self.template.base_dir,
                    )
            else:
                logger.warning(
                    "No import_resolver set, can't resolve local path for repository %s",
                    name,
                )
        else:  # import special (non-url) repositories like unfurl directly
            return name, url
        return "tosca_repositories." + name, name

    def convert_import(
        self, imp: Dict[str, str]
    ) -> Tuple[str, str, Tuple[str, str], str]:
        "converts tosca yaml import dict (as `imp`) to python import statement"
        repo = imp.get("repository")
        file = imp.get("file")
        namespace_prefix = imp.get("namespace_prefix")

        # file is required by TOSCA spec, so crash and burn if we don't have it
        assert file, "file is required for TOSCA imports"

        # figure out loading path
        filepath = PurePath(file)
        dirname = filepath.parent
        filename, tosca_name = self._get_name(filepath.stem)  # filename w/o ext

        base_dir = ""
        if repo:
            # generate repo import if repository: key given
            module_name, base_dir = self.find_repository(repo)
            # XXX add comment with repo url
            import_path = PurePath(base_dir)
        else:
            # otherwise assume local path
            assert self.template.path
            import_path = PurePath(self.template.path).parent
            # prefix module_name with . for relative path
            module_name = ""

        # import should be .path.to.file
        module_parts = module_name.split(".") + [
            "" if d == ".." else d for d in dirname.parts
        ]
        if filename == "__init__" and len(module_parts) > 1:
            # "from package import module" instead of "from package.module import __init__"
            filename = module_parts.pop()
        module_name = ".".join(module_parts)

        # generate import statement
        if namespace_prefix:
            # handle tosca namespace prefixes
            python_prefix, tosca_name = self._get_name(namespace_prefix)
            self.import_prefixes[namespace_prefix] = python_prefix
            if python_prefix != filename:
                if module_name.startswith("."):
                    # need an extra "."
                    from_name = "." + module_name
                else:
                    from_name = module_name
                import_stmt = (
                    f"from {from_name or '.'} import {filename} as {python_prefix}"
                )
            else:
                import_stmt = f"from {module_name or '.'} import {filename}"
        else:
            import_stmt = f"from {module_name}.{filename} import *"
            python_prefix = ""

        # add path to file in repo to repo path
        import_path = import_path / dirname / filepath.stem

        full_name = f"{module_name}.{filename}"
        return (
            import_stmt + "\n",
            os.path.normpath(str(import_path)),
            (full_name, python_prefix),
            base_dir,
        )

    def convert_types(
        self, tosca_types: dict, section: str, namespace_prefix="", indent=""
    ) -> str:
        src = ""
        self.namespace_prefix = namespace_prefix
        for name in tosca_types:
            if name == "unfurl.interfaces.Install":
                # special case
                if self._builtin_prefix != "tosca.":
                    src += (
                        indent
                        + """Install = tosca.interfaces.Install  # this is already defined because tosca.nodes.Root needs to inherit from it\n"""
                    )
                    continue
            elif self._builtin_prefix and not name.startswith(self._builtin_prefix):
                continue
            logger.info("converting type %s to python", name)
            try:
                toscatype = _make_typedef(name, self.custom_defs, True)
                if toscatype:
                    baseclass_name = section2typename(section)
                    self.imports.from_tosca.add(baseclass_name)
                    type_src = self.toscatype2class(toscatype, baseclass_name, indent)
                    src += self.flush_pending_defs(indent)
                    src += type_src + "\n\n"
                else:
                    logger.info("couldn't create type %s", name)
            except Exception:
                logger.error("error converting type %s to python", name, exc_info=True)
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
        elif (
            fullname == "unfurl.interfaces.Install" and self._builtin_prefix == "tosca."
        ):
            # special case when generating builtin tosca types, include this type too
            if minimize and self.namespace_prefix == "tosca.interfaces.":
                return "Install", fullname
            else:
                return "interfaces.Install", fullname
        elif self._builtin_prefix and fullname.startswith(self._builtin_prefix):
            return (
                self._builtin_name(fullname, self._builtin_prefix, minimize),
                fullname,
            )
        elif fullname in EntityType.TOSCA_DEF:  # unfurl built-in defs
            return fullname, ""
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

    def add_declaration(
        self, tosca_name: str, localname: Optional[str], include_in_all=True
    ):
        # new obj is being declared in the current module in the global scope
        if not localname:
            localname, _ = self._get_name(tosca_name)
        # handle conflicts because YAML can have different namespaces between templates types
        counter = 1
        basename = localname
        while localname in self.imports.declared:
            localname = basename + str(counter)
            counter += 1
        self.imports.add_declaration(tosca_name, localname, include_in_all)
        return localname

    def _set_name(
        self, yaml_name: str, fieldtype: str, prop_def=None
    ) -> Tuple[str, str, str]:
        name, toscaname = self._get_name(yaml_name)
        existing = self.local_names.get(name)
        if existing and existing != fieldtype:
            # conflict: name is used in another namespace
            toscaname = yaml_name  # set toscaname (which might be empty) because name is changing
            name = name + "_" + fieldtype  # rename to avoid conflict

        def _simple_typedef(prop_defs):
            if not prop_defs:
                return None
            ptype = prop_defs.get("type")
            if not ptype:
                return None
            if not prop_defs.get("required") and prop_defs.get("default") is None:
                return ptype + "| None"
            return ptype

        override = ""
        if self.has_overrides is None:  # in base init mode
            self.base_refs[name] = _simple_typedef(prop_def)
        elif name in self.base_refs:
            if not prop_def or _simple_typedef(prop_def) != self.base_refs[name]:
                override = "  # type: ignore[assignment]"
                self.has_overrides.append(name)
        self.local_names[name] = fieldtype
        return name, toscaname, override

    def python_name_from_type(self, tosca_type: str, minimize=False) -> str:
        # we assume the tosca_type has already been imported or is declared in this file
        qname = self.imports.get_local_ref(tosca_type)
        if not self._builtin_prefix and qname:
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
        return self._get_typed_value_repr(
            schema.schema["type"], schema.entry_schema, value, schema.metadata
        )[0]

    def _get_typed_value_repr(
        self, datatype: str, entry_schema, value: Any, metadata: Optional[dict] = None
    ) -> Tuple[str, bool]:
        if value is None:
            return "None", False
        if has_function(value):
            return self.value2python_repr(value), False
        typename = _tosca.TOSCA_SIMPLE_TYPES.get(datatype)
        if entry_schema:
            entry_schema = Schema(None, entry_schema)
            if typename == "Dict":
                items = [
                    f"{self.value2python_repr(k)}: {self._get_prop_value_repr(entry_schema, item)}"
                    for k, item in value.items()
                ]
                return "{" + ", ".join(items) + "}", True
            else:
                assert typename == "List"
                items = [
                    self._get_prop_value_repr(entry_schema, item) for item in value
                ]
                return "[" + ", ".join(items) + "]", True
        else:
            if datatype.startswith("scalar-unit."):
                if isinstance(value, (list, tuple)):
                    # for in_range constraints
                    return (
                        self._get_typed_value_repr("list", dict(type=datatype), value)[
                            0
                        ],
                        False,
                    )
                scalar_unit_class = get_scalarunit_class(datatype)
                assert scalar_unit_class
                default_unit = metadata and metadata.get("default_unit")
                if default_unit and not isinstance(value, str):
                    unit = cast(str, default_unit)
                else:
                    canonical = scalar_unit_class(value).validate_scalar_unit()
                    value, sep, unit = canonical.strip().partition(" ")
                self.imports.from_tosca.add(unit)
                return f"{value}*{unit}", False
            if typename:
                # simple value type
                if datatype in ["timestamp", "version"]:  # use wrapper type
                    self.imports.add_tosca_from(typename)
                    return f"{typename}({self.value2python_repr(value)})", False
                else:
                    return self.value2python_repr(value), False
            else:
                # its a tosca datatype
                typename, cls = self.imports.get_type_ref(datatype)
                if typename and cls:
                    assert cls and issubclass(cls, _tosca._BaseDataType), (
                        cls,
                        datatype,
                        typename,
                    )
                    dt = cls.get_tosca_datatype()
                else:
                    # hasn't been imported yet, must be declared in this file
                    typename, toscaname = self._get_name(datatype)
                    # use a TOSCA datatype
                    dt = DataType(datatype, self.custom_defs)
                    cls = None
                if dt.value_type:
                    # its a simple value type
                    return f"{typename}({self.value2python_repr(value)})", False
                if not isinstance(value, dict):
                    logger.error(
                        "expected a dict value for %s, got: %s", datatype, value
                    )
                    return str(value), False
                assert cls is None or issubclass(
                    cls, _tosca._BaseDataType
                )  # not a ValueType
                return self.convert_datatype_value(typename, cls, dt, value), True  # type: ignore

    def _constraint_args(self, c) -> str:
        if c.constraint_key == "in_range":
            min, max = c.constraint_value_msg
            return f"{self._get_typed_value_repr(c.property_type, None, min)[0]},{self._get_typed_value_repr(c.property_type, None, max)[0]}"
        else:
            return self._get_typed_value_repr(
                c.property_type, None, c.constraint_value_msg
            )[0]

    def to_constraints(self, constraints):
        # note: c.constraint_value_msg is unconverted value
        # constraint_key will correspond to constraint class names
        c = constraints[0]
        src_list = [
            f"{self.imports.add_tosca_from(c.constraint_key)}({self._constraint_args(c)})"
            for c in constraints
        ]
        if len(src_list) == 1:
            return f"({src_list[0]},)"
        return f"({', '.join(src_list)})"

    def _prop_type(self, schema: Schema) -> str:
        datatype = schema.schema["type"]
        typename = _tosca.TOSCA_SIMPLE_TYPES.get(datatype)
        if typename:
            if typename in __all__:
                self.imports.from_tosca.add(typename)
        else:
            # it's a tosca datatype
            datatype = _tosca.TOSCA_SHORT_NAMES.get(datatype, datatype)
            typename = self.maybe_forward_refs(self.python_name_from_type(datatype))[0]
        if schema.entry_schema:
            item_type_name = self._prop_type(Schema(None, schema.entry_schema))
        else:
            item_type_name = "Any"
        if typename == "Dict":
            typename += f"[str, {item_type_name}]"
        elif typename == "List":
            typename += f"[{item_type_name}]"

        if schema.constraints:
            if self.python_compatible >= 9:
                annotated_module = "typing"
            else:
                annotated_module = "typing_extensions"
            self.imports.add_import(annotated_module)
            typename = f"{annotated_module}.Annotated[{typename}, {self.to_constraints(schema.constraints)}]"
        return typename

    def _prop_decl(
        self, prop: Property, fieldtype: str, both: bool
    ) -> Tuple[str, str, str]:
        name, toscaname, overrides = self._set_name(
            prop.name, "property", prop.schema.schema
        )
        fieldparams = []
        if toscaname:
            fieldparams.append(f'name="{toscaname}"')
        default_value: Any = MISSING
        if "default" in prop.schema.schema:
            default_value = prop.schema.schema["default"]
        elif "value" in prop.schema.schema:  # special case for topology outputs
            default_value = prop.schema.schema["value"]
        elif not prop.required:
            default_value = None
        typename = self._prop_type(prop.schema)
        if not prop.required or default_value is None:
            typename = self._make_union(typename, "None")

        if prop.schema.title:
            fieldparams.append(
                f"title={self.value2python_repr(prop.schema.title, True)}"
            )
        if prop.schema.status:
            fieldparams.append(
                f"status={self.value2python_repr(prop.schema.status, True)}"
            )
        if prop.schema.metadata:
            fieldparams.append(f"metadata={metadata_repr(prop.schema.metadata)}")
        if both:
            fieldparams.append(f"attribute=True")

        if default_value is not MISSING:
            value_repr, mutable = self._get_typed_value_repr(
                prop.schema.schema["type"], prop.schema.entry_schema, default_value
            )
            if mutable or value_repr[0] in ("{", "["):
                fieldparams.append(f"factory=lambda:({value_repr})")
            elif fieldparams or fieldtype == "attributes":
                fieldparams.append(f"default={value_repr}")
            else:
                fielddecl = f"= {value_repr}{overrides}"
        else:
            fielddecl = overrides

        if fieldtype == "attributes":
            self.imports.add_tosca_from("Attribute")
            fielddecl = (
                f"= Attribute({inline_comment(overrides)}{', '.join(fieldparams)})"
            )
        elif fieldparams:
            self.imports.add_tosca_from("Property")
            fielddecl = (
                f"= Property({inline_comment(overrides)}{', '.join(fieldparams)})"
            )
        return name, typename, fielddecl  # type: ignore

    def _get_baseclass_names(
        self, entity_type: StatefulEntityType, baseclass_name: str
    ) -> str:
        parents = entity_type.parent_types()
        if not parents:
            base_names = baseclass_name
        else:
            base_names = ", ".join([
                self.python_name_from_type(p.type, True) for p in parents
            ])
        if baseclass_name == "DataEntity" and entity_type.defs:
            metadata = entity_type.defs.get("metadata")
            if metadata and metadata.get("additionalProperties"):
                # so OpenDataEntity.__init__ is used
                if base_names == "DataEntity":
                    base_names = "OpenDataEntity"
                else:
                    base_names = "OpenDataEntity, " + base_names
                self.imports.add_tosca_from("OpenDataEntity")

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

    def init_names(self, current_type: StatefulEntityType):
        # find all the identifiers declared on this type with the namespaces they appear in
        # namespace keys: requirement, capability, operation, property (includes attributes)
        self.local_names = {}
        self.base_refs = {}
        self.has_overrides = None
        for entity_type in reversed(current_type.ancestors()):
            # NB: order of _set_name calls needs to match toscatype2class()
            if entity_type.type == current_type.type:  # current is last
                break
            props = entity_type.get_definition("properties") or {}
            for name, prop_defs in props.items():
                self._set_name(name, "property", prop_defs)
            attrs = entity_type.get_definition("attributes") or {}
            for name, prop_defs in attrs.items():
                self._set_name(name, "property", prop_defs)
            if isinstance(entity_type, NodeType):
                capabilities = entity_type.get_capabilities_def()
                for name in capabilities:
                    self._set_name(name, "capability")
                reqs = entity_type.requirements or []
                for req in reqs:
                    name = list(req)[0]
                    self._set_name(name, "requirement")
                artifacts = entity_type.get_value("artifacts") or {}
                for name in artifacts:
                    self._set_name(name, "artifact")
            for iname, idef in entity_type.interfaces.items():
                ops = idef.get("operations") or {}
                for name in ops:
                    self._set_name(name, "operation")
        self.has_overrides = []

    def toscatype2class(
        self, toscatype: StatefulEntityType, baseclass_name: str, initial_indent=""
    ) -> str:
        indent = "    "
        self.init_names(toscatype)
        # XXX list of imports
        toscaname = toscatype.type
        if self._builtin_prefix:
            cls_name = self.python_name_from_type(toscaname, True)
        else:
            cls_name = self._get_name(toscaname, True)[0]
            cls_name = self.add_declaration(toscaname, cls_name)
        base_names = self._get_baseclass_names(toscatype, baseclass_name)
        metadata = toscatype.defs and toscatype.defs.get("metadata")
        if metadata and metadata.get("alias"):
            assert "," not in base_names
            if self.python_compatible > 9:
                annotated_module = "typing"
            else:
                annotated_module = "typing_extensions"
            self.imports.add_import(annotated_module)
            return f"{initial_indent}{cls_name}: {annotated_module}.TypeAlias = {base_names}"
        simple_type = cast(str, toscatype.get_value("type"))
        if simple_type and simple_type in _tosca.TOSCA_SIMPLE_TYPES:
            # its a value datatype
            base_names = "tosca.ValueType, " + _tosca.TOSCA_SIMPLE_TYPES[simple_type]
        class_decl = f"{initial_indent}class {cls_name}({base_names}):"
        src = ""
        indent = initial_indent + indent
        # XXX: 'version'
        assert toscatype.defs
        src += add_description(toscatype.defs, indent)
        if toscaname != cls_name:
            src += f'{indent}_type_name = "{toscaname}"\n'
        if metadata:
            formatted = textwrap.indent(
                metadata_repr(metadata),
                indent + indent,
            )
            src += f"{indent}_type_metadata = {formatted}\n"

        version = toscatype.defs and toscatype.defs.get("version") or None
        if version is not None:
            self.imports.from_tosca.add("tosca_version")
            src += f'{indent}_version = "tosca_version({self.value2python_repr(str(version), True)})\n'

        src += self.add_properties_decl(toscatype, "properties", indent)
        src += self.add_properties_decl(toscatype, "attributes", indent)

        caps = toscatype.get_value("capabilities")
        if caps:
            for name, tpl in caps.items():
                src += self.add_capability(name, tpl, indent)

        if isinstance(toscatype, NodeType):
            reqs = toscatype.requirement_definitions
            for tpl in toscatype.get_value("requirements") or []:
                assert tpl, tpl
                req_name, req = list(tpl.items())[0]
                # get the full req including inherited values
                src += self.add_req(req_name, reqs[req_name], indent, cls_name)
            artifacts: Dict[str, Artifact] = {}
            required_artifacts: Dict[str, dict] = {}
            NodeTemplate.find_artifacts_on_type(
                toscatype, artifacts, required_artifacts, False
            )
            for artifact in artifacts.values():
                artifact_name, artifact_src = self.artifact2obj(artifact)
                if artifact_src:
                    field_name, tosca_name, overrides = self._set_name(
                        artifact_name, "artifact"
                    )
                    assert artifact.type
                    cls_name, cls = self.imports.get_type_ref(artifact.type)
                    assert cls_name
                    src += f"{indent}{field_name}: {cls_name} = {artifact_src}\n"
            for (
                required_artifact_name,
                required_artifact_tpl,
            ) in required_artifacts.items():
                cls_name, cls = self.imports.get_type_ref(
                    required_artifact_tpl.get("type", "")
                )
                if cls_name:
                    name, _ = self._get_name(required_artifact_name)
                    field_name, tosca_name, overrides = self._set_name(name, "artifact")
                    # XXX what to do if field_name != tosca_name?
                    required = required_artifact_tpl.get("required")
                    if required:
                        src += f"{indent}{field_name}: {cls_name}{overrides}\n"
                    else:
                        src += f"{indent}{field_name}: {self._make_union(cls_name, 'None')} = None{overrides}\n"

        if baseclass_name == "Interface":
            # inputs and operations are defined directly on the body of the type
            for op in create_operations({toscaname: toscatype.defs}, toscatype, None):
                # XXX this will have skipped not_implemented operations but they need to converted too
                _, op_src = self.operation2func(op, indent, [])
                src += op_src + "\n"
        else:
            # 3.7.5.2 operation definitions.
            _, ops_src = self._add_operations(indent, toscatype, None)
            src += ops_src

        target_types = toscatype.get_value("valid_target_types")
        if target_types:
            python_target_types = [self.python_name_from_type(t) for t in target_types]
            src += f"{indent}_valid_target_types = [{', '.join(python_target_types)}]\n"

        # artifact, relationship and datatype types have special keys
        for key in ["file_ext", "mime_type", "constraints", "default_for"]:
            value = toscatype.get_value(key)
            if value:
                src += f"{indent}_{key} = {self.value2python_repr(value, True)}\n"

        class_decl += "\n"

        if src.strip():
            return class_decl + src
        else:
            return class_decl + f"{indent}pass"

    def _add_operations(
        self,
        indent: str,
        nodetype: Optional[StatefulEntityType],
        template: Optional[EntityTemplate],
    ) -> Tuple[List[str], str]:
        src = ""
        names = []
        default_ops = []
        if nodetype:
            for iname, interface in nodetype.interfaces.items():
                if "operations" in interface:
                    ops = interface["operations"]
                else:
                    ops = interface
                for oname, op in ops.items():
                    if not op:
                        default_ops.append((iname, oname))
        else:
            assert template
            # get an empty type without any operations defined
            nodetype = find_type("tosca.nodes.Root", template.custom_def)
            assert nodetype

        entity_tpl = template.entity_tpl if template else None
        defaulted = False
        declared_ops = []
        declared_default_ops = []
        declared_interfaces: Optional[Dict] = nodetype.get_value(
            "interfaces", entity_tpl
        )
        declared_requirements = []
        declared_default = False
        if declared_interfaces:
            for iname, interface in declared_interfaces.items():
                requirements = interface.get("requirements")
                if requirements:
                    declared_requirements.extend(requirements)
                if "operations" in interface:
                    ops = interface["operations"]
                else:
                    ops = interface
                if iname == "default":
                    declared_default = True
                for oname, op in ops.items():
                    if op:
                        declared_ops.append((iname, oname))
                    else:
                        declared_default_ops.append((iname, oname))
        if not template and declared_requirements:
            # include inherited interface_requirements
            src += f"{indent}_interface_requirements = {self.value2python_repr(nodetype.get_interface_requirements(entity_tpl))}\n"

        for op in cast(
            List[OperationDef], EntityTemplate._create_interfaces(nodetype, template)
        ):
            # XXX this will have skipped not_implemented operations but they need to be converted too
            if op.interfacetype != "Mock":  # XXX handle Mock
                op_id = (op.interfacename, op.name)
                if op_id in declared_ops and op_id not in default_ops:
                    name, op_src = self.operation2func(
                        op, indent, default_ops, template and template.name
                    )
                    names.append(name)
                    src += op_src + "\n"
                elif op.name == "default" and not defaulted and default_ops:
                    # only add default operation if it was declared on this class
                    # or this class declared operations that used the default (so needs to override the decorator's apply_to)
                    if declared_default_ops or declared_default:
                        defaulted = True  # only generate once
                        name, op_src = self.operation2func(
                            op, indent, default_ops, template and template.name
                        )
                        if name:
                            names.append(name)
                        src += op_src + "\n"
        return names, src

    def add_properties_decl(
        self, entity_type: StatefulEntityType, fieldname: str, indent: str
    ) -> str:
        src = ""
        declared_props = entity_type.get_value(fieldname)
        if not declared_props:
            return ""
        props = entity_type.get_definition(fieldname)
        if fieldname == "attributes":
            shadowed = entity_type.get_value("properties") or {}
        else:
            shadowed = entity_type.get_value("attributes") or {}
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
            propdef = PropertyDef(name, None, schema)
            prop = Property(propdef.name, None, propdef.schema, self.custom_defs)
            name, typedecl, fielddecl = self._prop_decl(prop, fieldname, both)
            if self._include_typedecl(name, entity_type):
                src += f"{indent}{name}: {typedecl} {fielddecl}\n"
            else:
                src += f"{indent}{name} {fielddecl}\n"
            src += add_description(propdef.schema, indent)
        if src:
            src += "\n"
        return src

    def _include_typedecl(self, name: str, entity_type: StatefulEntityType) -> bool:
        if (
            isinstance(entity_type, ArtifactTypeDef)
            and name in _tosca.ArtifactEntity._builtin_fields
        ):  # don't redefine type of built-in fields
            return False
        return True

    def _set_arity(self, typedecl, _min, _max, default: str) -> Tuple[str, str]:
        if _max == "UNBOUNDED" or _max > 1:
            typedecl = f"Sequence[{typedecl}]"  # use sequence for covariance
            if default:
                default = f"({default},)"
            elif _min == 0:
                default = "()"
        elif _min == 0:
            typedecl = self._make_union(typedecl, "None")
            default = "None"
        return typedecl, default

    def add_capability(self, name, tpl, indent) -> str:
        # Capability(factory=typename) (if no required properties) or default=None or ()
        fieldparams = []
        name, toscaname, overrides = self._set_name(name, "capability")
        if toscaname:
            fieldparams.append(f'name="{toscaname}"')
        cap_type_name = self.python_name_from_type(tpl["type"])
        typedecl = self.maybe_forward_refs(cap_type_name)[0]
        default = (
            ""  # XXX if properties, default is factory: lambda: CapabilityType(props)
        )
        if "occurrences" in tpl:
            min, max = tpl["occurrences"]
            typedecl, default = self._set_arity(typedecl, min, max, default)
        if default:
            fieldparams.append("default=" + default)
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
                f"valid_source_types={self.value2python_repr(valid_source_types, True)}"
            )
        metadata = tpl.get("metadata")
        if metadata:
            fieldparams.append(f"metadata={metadata_repr(metadata)}")
        if fieldparams:
            self.imports.add_tosca_from("Capability")
            fielddecl = (
                f"= Capability({inline_comment(overrides)}{', '.join(fieldparams)})\n"
            )
        else:
            fielddecl = overrides
        src = f"{indent}{name}: {typedecl} {fielddecl}\n"
        src += add_description(tpl, indent)
        return src

    def _get_req_types(
        self, req: dict, inline_name: str
    ) -> Tuple[List[str], str, bool]:
        types: List[str] = []
        relationship = req.get("relationship")
        default = ""
        topology: Dict[str, Any] = self.topology
        if relationship:
            if isinstance(relationship, dict):
                if len(relationship) > 1:
                    default = self.template_reference(
                        inline_name,
                        "relationship",
                        RelationshipTemplate(relationship, "", self.custom_defs),
                    )
                relationship = relationship["type"]
            elif relationship in topology.get("relationship_templates", {}):
                reltpl = cast(dict, topology["relationship_templates"][relationship])
                default = self.template_reference(relationship, "relationship")
                relationship = reltpl["type"]
            if relationship != "tosca.relationships.Root":
                types.append(relationship)

        nodetype = req.get("node")
        if nodetype:
            # req['node'] can be a node_template instead of a type
            if (
                topology.get("node_templates")
                and nodetype in topology["node_templates"]
            ):
                entity_tpl = cast(dict, topology["node_templates"][nodetype])
                match = self.template_reference(nodetype, "node")
                if default:
                    default += f"[{match}]"
                else:
                    default = match
                nodetype = entity_tpl["type"]
            types.append(expand_prefix(nodetype))

        cap = req.get("capability")
        if cap:
            # if no other types then set flag to add requirement() in order to distinguish this from a capability
            explicit = not bool(types)
            types.append(cap)
        else:
            explicit = False
        return types, default, explicit

    def add_req(self, req_name: str, req: dict, indent: str, typename: str) -> str:
        if isinstance(req, str):
            req = dict(node=req)
        name, toscaname, overrides = self._set_name(req_name, "requirement")
        types, match, explicit = self._get_req_types(
            req, f"_inline_relationship_{typename}_{name}"
        )
        # XXX add rel.valid_target_types
        types = self.import_types(types)
        if not types:
            # XXX need to merge with base requirements
            logger.error("req missing types %s", req)
            return ""
        if len(types) > 1:
            typedecl = self._make_union(*types)
        else:
            typedecl = types[0]

        if "occurrences" in req:
            min, max = req["occurrences"]
            typedecl, default = self._set_arity(typedecl, min, max, match)
        else:
            default = match

        fieldparams = []
        if toscaname:
            fieldparams.append(f'name="{toscaname}"')
        node_filter = req.get("node_filter")
        if node_filter:
            fieldparams.append(
                f"node_filter={self.value2python_repr(node_filter, True)}"
            )
        metadata = req.get("metadata")
        if metadata:
            metadata.pop("before_patch", None)
            fieldparams.append(f"metadata={metadata_repr(metadata)}")
        if fieldparams or explicit:
            if default:
                fieldparams.insert(0, "default=" + default)
            self.imports.add_tosca_from("Requirement")
            fielddecl = (
                f"= Requirement({inline_comment(overrides)}{', '.join(fieldparams)})"
            )
        elif default:
            fielddecl = "= " + default + overrides
        else:
            fielddecl = overrides
        src = f"{indent}{name}: {typedecl} {fielddecl}\n"
        src += add_description(req, indent)
        return src

    def get_configurator_decl(self, op: OperationDef) -> Tuple[str, Dict[str, Any]]:
        if op.invoke:
            return f"self.{op.invoke.split('.')[-1]}", dict(inputs=op.inputs)
        if not op._source:
            op._source = self.base_dir
        kw = (
            self.template.import_resolver.find_implementation(op)
            if self.template.import_resolver
            else None
        )
        cmd = ""
        if kw is None or kw["primary"]:
            if isinstance(op.implementation, dict):
                artifact = op.implementation.get("primary")
                kw = op.implementation.copy()
            else:
                artifact = op.implementation
                kw = dict(primary=artifact)
            kw["inputs"] = op.inputs
            if isinstance(artifact, str):
                artifact, toscaname = self._get_name(artifact)
                tname = toscaname or artifact
                if op.node_template and tname in op.node_template.artifacts:
                    # add direct reference to allow static type checking
                    cmd = f"self.{artifact}.execute"
            if not cmd and artifact:
                cmd = f"self.find_artifact({self.value2python_repr(artifact)}).execute"
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
        return cmd, kw

    def operation2func(
        self,
        op: OperationDef,
        indent: str,
        default_ops: List[Tuple[str, str]],
        template_name: Any = "",
    ) -> Tuple[str, str]:
        # iDef.entry_state: add to decorator
        # note: defaults and base class inputs and implementations already merged in
        # artifact property reference or configurator class
        if op.name == "default":
            if not op.implementation and (op.input_defs or op.inputs):
                src = f"{indent}_{op.interfacename.split('.')[-1]}_default_inputs = {self.value2python_repr(op.input_defs or op.inputs)}"
                return "", src
        configurator_decl, kw = self.get_configurator_decl(op)
        op_name, toscaname, _ = self._set_name(cast(str, op.name), "operation")
        if template_name:
            template_name, _ = self._get_name(template_name)
            func_name = f"{template_name}_{op_name}"
        else:
            func_name = op_name
        src = ""
        decorator = []
        if toscaname:
            decorator.append(f'name="{toscaname}"')
        elif template_name:
            decorator.append(f'name="{op.name}"')
        if op.name == "default":
            apply_to = ", ".join([f'"{op[0]}.{op[1]}"' for op in default_ops])
            decorator.append(f"apply_to=[{apply_to}]")
        for imp_key in (
            "timeout",
            "operation_host",
            "environment",
            "outputs",
            "dependencies",
            "entry_state",
            "invoke",
        ):
            imp_val = kw.get(imp_key)
            if imp_val is not None:
                if (
                    imp_key != "dependencies" or imp_val
                ):  # dependencies is always a list, skip if empty
                    decorator.append(f"{imp_key}={self.value2python_repr(imp_val)}")
        if not configurator_decl:
            self.imports.add_tosca_from("operation")
            src += f"{indent}{op_name} = operation({', '.join(decorator)})\n"
            src += add_description(op.value, indent)
            return op_name, src

        if decorator:  # add decorator
            self.imports.add_tosca_from("operation")
            src += f"{indent}@operation({', '.join(decorator)})\n"

        # XXX add arguments declared on the interface definition
        # XXX declare configurator/artifact as the return value
        type_anno = ": Any" if not self.concise else ""
        args = f"self, **kw{type_anno}"
        type_anno = " -> Any" if not self.concise else ""
        src += f"{indent}def {func_name}({args}){type_anno}:\n"
        indent += "   "
        desc = add_description(op.value, indent)
        src += desc
        src += f"{indent}return {configurator_decl}("
        # all on one line for now
        inputs = kw["inputs"]
        if inputs:
            src += "\n"
            for name, value in inputs.items():
                # use encode_identifier to handle input names that aren't valid python identifiers
                src += f"{indent}{indent}{encode_identifier(name)} = {self.value2python_repr(value)},"
            src += f"{indent})\n"
        else:
            src += ")\n"
        return op_name, src

    def _get_prop_init_list(
        self,
        props,
        prop_defs,
        cls: Optional[Type[_tosca.ToscaType]],
        indent="",
        include_additional=False,
    ) -> Tuple[str, Dict[str, str], Dict[str, PropertyDef]]:
        src = ""
        skipped: Dict[str, str] = {}
        if not props:
            return src, skipped, {}
        prop_defs = prop_defs.copy()
        for key, val in props.items():
            prop = prop_defs.pop(key, None)
            if prop:
                if not isinstance(prop.schema, Schema):
                    schema = Schema(key, prop.schema)
                else:
                    schema = prop.schema
                prop_repr = self._get_prop_value_repr(schema, val)
            else:
                prop_repr = self.value2python_repr(val)
            if cls:
                field = cls.get_field_from_tosca_name(key, ToscaFieldType.property)
                if field:
                    field_name = field.name
                else:
                    if include_additional:
                        field_name, tosca_name = self._get_name(key)
                    else:
                        skipped[key] = prop_repr
                        continue
            else:
                field_name, tosca_name = self._get_name(key)
            src += f"{indent}{field_name}={prop_repr},\n"
        return src, skipped, prop_defs

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
        include_additional = bool(
            cls
            and cls._type_metadata
            and cls._type_metadata.get("additionalProperties")
        )
        init_list, skipped, _ = self._get_prop_init_list(
            value, props, cls, indent, include_additional
        )
        if skipped:
            logger.warning(
                f"Additional properties on datatype {classname} skipped: {skipped}"
            )
        src += init_list + ")"
        return src

    def _get_capability(
        self,
        capability_type,
        values: Dict[str, Any],
        indent="",
    ) -> str:
        typename, cls = cast(
            Tuple[str, Type[_tosca.CapabilityEntity]],
            self.imports.get_type_ref(capability_type.type),
        )
        src = f"{indent}{typename}("
        prop_defs = capability_type.get_properties_def()
        init_list, skipped, _ = self._get_prop_init_list(values, prop_defs, cls, indent)
        if skipped:
            logger.warning(
                f"Additional properties on capability {typename} skipped: {skipped}"
            )
        src += init_list + ")"
        return src

    def flush_pending_defs(self, indent="") -> str:
        if self._pending_defs:
            src = indent + f"\n{indent}".join(self._pending_defs) + "\n"
            self._pending_defs = []
            return src
        return ""

    def template_reference(self, tosca_name: str, type: str, template=None) -> str:
        # return "" if tosca_name is a type name
        localname = self.imports.get_local_ref(tosca_name)
        if not localname:
            assert self.template.topology_template
            src = ""
            if type == "node":
                if not template:
                    template = self.template.topology_template.node_templates.get(
                        tosca_name
                    )
                if template:
                    localname, src = self.node_template2obj(template, indent="")
                if not template or not localname:
                    if self.is_tosca_type(tosca_name):
                        return ""
                    else:
                        logger.warning(
                            f'Node template "{tosca_name}" not found in topology, using find_node("{tosca_name}") instead of converting to Python.'
                        )
                        return f'tosca.find_node("{tosca_name}")'
            elif type == "relationship":
                if not template:
                    template = (
                        self.template.topology_template.relationship_templates.get(
                            tosca_name
                        )
                    )
                if template:
                    localname, src = self.relationship_template2obj(template, indent="")
                    if not localname and src:
                        # template was inline and unnamed,
                        # use the given name as the variable name if set
                        if tosca_name:
                            localname = tosca_name
                            src = f"{localname} = {src}"
                        else:  # no given name, include definition inline
                            return src
                if not template or not localname:
                    if self.is_tosca_type(tosca_name):
                        return ""
                    else:
                        logger.warning(
                            f'Relationship template conversion not found in topology, using find_relationship("{tosca_name}") instead of converting to Python.'
                        )
                        return f'tosca.find_relationship("{tosca_name}")'
            else:
                logger.error(f"templates of type {type} not supported")
                return tosca_name

            # we need insert the code declaring this template before its name is referenced
            if src:
                self._pending_defs.append(src)
        return localname

    def template2obj(
        self,
        entity_template: EntityTemplate,
        indent: str,
        declare: str,
    ) -> Tuple[Optional[Type[_tosca.ToscaType]], str, str, Dict[str, str]]:
        self.local_names = {}
        skipped: Dict[str, str] = {}
        assert entity_template.type
        cls_name, cls = self.imports.get_type_ref(entity_template.type)
        assert cls is None or issubclass(cls, _tosca.ToscaType)
        if not cls_name:
            logger.error(
                f"could not convert template {entity_template.name}: {entity_template.type} wasn't imported"
            )
            return None, "", "", skipped
        elif not cls:
            logger.error(
                f"could not convert template {entity_template.name}: {entity_template.type} is defined in current file so the compiled class isn't available"
            )
            # XXX compile and exec the source code generated so far
            return None, "", "", skipped
        # use substitute_node and select_node since __init__() might be missing required arguments
        if "select" in entity_template.directives:
            self.imports.add_tosca_from("select_node")
            decl = f"select_node({cls_name}, "
        elif "substitute" in entity_template.directives:
            self.imports.add_tosca_from("substitute_node")
            decl = f"substitute_node({cls_name}, "
        else:
            decl = f"{cls_name}("
        if entity_template.name and declare:
            # XXX names should be from parent namespace (module or Namespace)
            # don't include templates in __all__
            name = self.add_declaration(entity_template.name, None, False)
            logger.info("converting template %s to python", name)
            src = f"{indent}{name}: {declare} = {decl}"
            self.imports.from_tosca.add(declare)
        else:
            name, _ = self._get_name(entity_template.name)
            src = decl
        # always add name because we might not have access to the name reference
        src += f'"{entity_template.name}", '
        entity_tpl = entity_template.entity_tpl
        metadata = entity_tpl.get("metadata")
        if metadata:
            before_patch = metadata.pop("before_patch", None)
            if before_patch:
                entity_tpl = before_patch
            src += f"_metadata={metadata_repr(metadata)},\n"
        directives = entity_template.directives.copy()
        assert entity_template.type_definition
        properties = entity_tpl.get("properties")
        init_list = ""
        if properties:
            prop_defs = entity_template.type_definition.get_properties_def()
            init_list, skipped, missing = self._get_prop_init_list(
                properties, prop_defs, cls, indent
            )
            required = [
                pdef.name
                for pdef in missing.values()
                if pdef.required and pdef.default is None
            ]
            if required and "partial" not in directives:
                directives.append("partial")
        if directives and isinstance(entity_template, NodeTemplate):
            src += f"_directives={repr(directives)},\n"
        if init_list:
            src += init_list
        node_filter = entity_tpl.get("node_filter")
        if node_filter:
            src += f"_node_filter={repr(node_filter)},\n"
        return cls, name, src, skipped

    def artifact2obj(self, artifact: Artifact, indent="") -> Tuple[str, str]:
        cls, name, src, skipped = self.template2obj(artifact, indent, "")
        if not cls:
            return "", ""
        src += "file=" + self.value2python_repr(artifact.file) + ", "  # type: ignore
        for field in _tosca.ArtifactEntity._builtin_fields[1:]:
            if field in skipped:
                val = skipped.pop(field)
            else:
                val = getattr(artifact, field)
                if val:
                    val = self.value2python_repr(val)
            if val:
                src += f"{field}={val},\n"
        src += ")"  # close ctor
        func_indent = "   "
        add_src = self.add_assignments(artifact, name, skipped, func_indent, indent)
        if add_src:
            assert artifact.type
            cls_name, cls = self.imports.get_type_ref(artifact.type)  # type: ignore
            func_name = f"_make_{name}"
            func_src = f"def {func_name}() -> {cls_name}:\n{func_indent}{name} = {src}\n{add_src}\n{func_indent}return {name}\n"
            self._pending_defs.append(func_src)
            return name, f"{func_name}()"
        return name, src

    def add_assignments(
        self, template, name, skipped, indent, pending_indent=""
    ) -> str:
        src = self.add_additional_properties(indent, name, skipped)
        src += self.add_template_description(template, indent, name)
        src += self.add_template_interfaces(template, indent, name, pending_indent)
        return src

    def relationship_template2obj(
        self, template: RelationshipTemplate, indent=""
    ) -> Tuple[str, str]:
        cls, name, src, skipped = self.template2obj(template, indent, "Relationship")
        if not cls:
            return "", ""
        if template.default_for:
            src += f"_default_for={self.value2python_repr(template.default_for)}"
        src += ")"  # close ctor
        src += self.add_assignments(template, name, skipped, indent)
        return name, src

    def node_template2obj(
        self, node_template: NodeTemplate, indent=""
    ) -> Tuple[str, str]:
        cls, name, src, skipped = self.template2obj(node_template, indent, "Node")
        if not cls:
            return "", ""
        # note: the toscaparser doesn't support declared attributes currently
        capabilities = node_template.entity_tpl.get("capabilities")
        if capabilities:
            # only get explicitly declared capability properties
            capabilitydefs = cast(
                NodeType, node_template.type_definition
            ).get_capabilities_def()
            for cap_name, capability in capabilities.items():
                cap_props = capability.get("properties")
                field = cls.get_field_from_tosca_name(
                    cap_name, ToscaFieldType.capability
                )
                if field:
                    field_name = field.name
                else:
                    field_name, tosca_name, _ = self._set_name(cap_name, "capability")
                src += f"{field_name}={self._get_capability(capabilitydefs[cap_name], cap_props, indent)},\n"

        template_reqs = []  # requirements not declared by the type
        requirements: Dict[_tosca._Tosca_Field, List[str]] = {}
        for reqitem in node_template.requirements:
            req_name, req = list(reqitem.items())[0]
            req_assignment = self._get_req_assignment(req)
            if req_assignment:
                field = cls.get_field_from_tosca_name(
                    req_name, ToscaFieldType.requirement
                )
                if field:
                    requirements.setdefault(field, []).append(req_assignment)
                else:
                    template_reqs.append((encode_identifier(req_name), req_assignment))
        for field, assignments in requirements.items():
            typeinfo = field.get_type_info()
            if len(assignments) == 1 and not typeinfo.collection:
                src += f"{field.name}={assignments[0]},\n"
            else:
                src += f"{field.name}=[{', '.join(assignments)}],\n"
        artifacts = []
        for artifact_tosca_name, artifact in node_template.artifacts.items():
            artifacts_tpl = node_template.entity_tpl.get(node_template.ARTIFACTS)
            if not artifacts_tpl or artifact_tosca_name not in artifacts_tpl:
                continue  # defined on the type so skip
            artifact_name, artifact_src = self.artifact2obj(artifact)
            if artifact_src:
                field = cls.get_field_from_tosca_name(
                    artifact_tosca_name, ToscaFieldType.artifact
                )
                if field:
                    src += f"{artifact_name}={artifact_src},\n"
                else:
                    artifacts.append((artifact_name, artifact_src))
        src += ")\n"  # close ctor
        src += self.add_additional_properties(indent, name, skipped)
        src += self.add_template_description(node_template, indent, name)
        # add these as attribute statements
        # (use setattr to avoid mypy complaints about attribute not defined)
        for artifact_name, artifact_src in artifacts:
            if self.assign_attr:
                src += f"{indent}__{name}_{artifact_name}: ArtifactEntity = {artifact_src}\n"
                self.imports.from_tosca.add("ArtifactEntity")
                # use local name because black will put the type: ignore comment on the wrong line if artifact_src is too long for one line.
                src += f"{indent}{name}.{artifact_name} = __{name}_{artifact_name}  # type: ignore[attr-defined]\n"
            else:
                src += f"{indent}setattr({name}, '{artifact_name}', {artifact_src})\n"
        for req_name, req_assignment in template_reqs:
            if self.assign_attr:
                src += f"{indent}{name}.{req_name} = {req_assignment}  # type: ignore[attr-defined]\n"
            else:
                src += f"{indent}setattr({name}, '{req_name}', {req_assignment})\n"
        src += self.add_template_interfaces(node_template, indent, name, indent)
        return name, src

    def add_additional_properties(self, indent, name, skipped) -> str:
        src = ""
        for prop_name, prop_value in skipped.items():
            if self.assign_attr:
                src += f"{indent}{name}.{prop_name} = {prop_value}  # type: ignore[attr-defined]\n"
            else:
                src += f"{indent}setattr({name}, '{prop_name}', {prop_value})\n"
        return src

    def add_template_description(self, template, indent, name) -> str:
        src = ""
        description = template.entity_tpl.get("description")
        if description and description.strip():
            src += f"{indent}{name}._description = " + add_description(
                template.entity_tpl, indent
            )
        return src

    def add_template_interfaces(
        self, node_template, indent, name, pending_indent
    ) -> str:
        src = ""
        names, ops_src = self._add_operations(pending_indent, None, node_template)
        if names:
            self._pending_defs.append(ops_src)
            for op_name in names:
                src += f'{indent}{name}.set_operation({name}_{op_name}, "{op_name}")\n'
        return src

    def _get_req_assignment(self, req):
        node = None
        req_assignment = None
        field_args = []
        if isinstance(req, str):
            node = req
            capability = None
            relationship = None
            node_filter = None
        else:
            node = req.get("node")
            capability = req.get("capability")
            relationship = req.get("relationship")
            node_filter = req.get("node_filter")
        if node:
            req_assignment = self.template_reference(node, "node")
            if not req_assignment:
                field_args.append(f'node="{node}"')
            if capability:
                # XXX get target template object and look up python attribute name for capability
                req_assignment += f".{capability}"
        if relationship:
            if isinstance(relationship, str):
                rel_assignment = self.template_reference(relationship, "relationship")
            else:
                rel_assignment = self.template_reference(
                    "",
                    "relationship",
                    RelationshipTemplate(relationship, "", self.custom_defs),
                )
            if rel_assignment:
                if req_assignment:
                    req_assignment = f"{rel_assignment}[{req_assignment}]"
                else:
                    req_assignment = rel_assignment
        if node_filter:
            field_args.append(f"node_filter={self.value2python_repr(node_filter)}")
        if field_args:
            if req_assignment:
                field_args.insert(0, f"default={req_assignment}")
            self.imports.add_tosca_from("Requirement")
            return f"Requirement({', '.join(field_args)})"
        return req_assignment

    def follow_import(
        self,
        import_def: dict,
        import_path: str,
        format: bool,
        base_dir,
        converted: Optional[Set[str]],
    ) -> None:
        # the ToscaTemplate has already imported everything, so here we just need to get the import's contents
        # to convert it to Python
        file_path = str(Path(import_path).parent / Path(import_def["file"]).name)
        if file_path not in self.template.nested_tosca_tpls:
            file_path = os.path.abspath(file_path)
            if file_path not in self.template.nested_tosca_tpls:
                logger.warning(
                    f"can't import: {file_path} not found in {list(self.template.nested_tosca_tpls)}"
                )
                return

        assert self.template.tpl is not None
        tpl, namespace_id = self.template.nested_tosca_tpls[file_path]
        if isinstance(self.custom_defs, Namespace):
            custom_defs = self.custom_defs.find_namespace(namespace_id)
        else:
            custom_defs = self.custom_defs
        file_path = os.path.abspath(file_path)
        # make sure the content of the import has the tosca version header and all repositories
        tpl["tosca_definitions_version"] = self.template.tpl[
            "tosca_definitions_version"
        ]
        if "repositories" in self.template.tpl:
            repositories = self.template.tpl["repositories"]
            tpl.setdefault("repositories", {}).update(repositories)
        repository = import_def.get("repository")
        if repository:
            package = "tosca_repositories." + re.sub(r"\W", "_", repository)
        else:
            package = "service_template"
        if repository == "unfurl" and not self.convert_built_in:
            logger.debug("not converting built-in import: %s", import_path)
        elif self.write_policy.can_overwrite(file_path, import_path):
            convert_service_template(
                ToscaTemplate(
                    file_path,
                    yaml_dict_tpl=tpl,
                    import_resolver=self.template.import_resolver,
                    verify=False,
                    base_dir=base_dir or self.base_dir,
                ),
                self.python_compatible,
                self._builtin_prefix,
                format,
                custom_defs=custom_defs,
                path=import_path,
                write_policy=self.write_policy,
                base_dir=base_dir or self.base_dir,
                package_name=package,
                converted=converted,
            )
        else:
            logger.info(
                "not converting %s: %s", import_path, self.write_policy.deny_message()
            )

    def get_package_name(self) -> str:
        path = self.template.path
        assert path
        try:
            package_path = Path(path).parent.relative_to(self.base_dir)
            relpath = str(package_path).strip("/").replace("/", ".").strip(".")
            package = self.package_name
            if relpath:
                package += "." + relpath
        except ValueError:
            package = "tosca_repositories." + os.path.basename(os.path.dirname(path))
        return package

    def execute_source(self, src: str, namespace: Mapping[str, Any]):
        package = self.get_package_name()
        assert self.template.path
        full_name = package + "." + re.sub(r"\W", "_", Path(self.template.path).stem)
        try:
            result = loader.restricted_exec(
                self.imports.prelude() + src, namespace, self.base_dir, full_name
            )
            self.imports._add_imports("", namespace)
        except:
            # print(self.imports.prelude() + src)
            logger.error(
                f"error executing generated source for {full_name} in {self.base_dir}",
                exc_info=True,
            )
        finally:
            # not in safe_mode, delete from sys.modules if present since source might not be complete
            sys.modules.pop(full_name, None)

    def convert_repository(
        self, toscaname: str, repositories: Mapping[str, Any]
    ) -> str:
        self.imports.from_tosca.add("Repository")
        name = self.add_declaration(toscaname, None)
        repo_args = [f"name={self.value2python_repr(toscaname)}"]
        for key, value in repositories.items():
            if key == "credential":
                credential_src = "credential=tosca.datatypes.Credential("
                for key, value in value.items():
                    credential_src += f"{key}={self.value2python_repr(value)}, "
                repo_args.append(credential_src + ")")
            else:
                repo_args.append(f"{key}={self.value2python_repr(value)}")
        return f"{name} = Repository({', '.join(repo_args)})\n"


def generate_builtins(import_resolver, format=True) -> str:
    custom_defs = EntityType.TOSCA_DEF.copy()
    tosca_template = ToscaTemplate(
        path=EntityType.TOSCA_DEF_FILE,
        yaml_dict_tpl=EntityType.TOSCA_DEF_LOAD_AS_IS,
        import_resolver=import_resolver,
    )
    # stupid side-effect: loads definitions
    tosca_template._validate_version("tosca_simple_unfurl_1_0_0")
    tosca_template.tpl["interface_types"]["unfurl.interfaces.Install"] = custom_defs[
        "unfurl.interfaces.Install"
    ] = EntityType.TOSCA_DEF["unfurl.interfaces.Install"]
    return convert_service_template(
        tosca_template,
        7,
        "tosca.",
        format,
        custom_defs,
    )


def generate_builtin_extensions(import_resolver, format=True) -> str:
    def_path = ToscaTemplate.exttools.get_defs_file("tosca_simple_unfurl_1_0_0")
    return convert_service_template(
        ToscaTemplate(path=def_path, import_resolver=import_resolver),
        7,
        f"unfurl.",
        format,
        EntityType.TOSCA_DEF,
    )


def yaml_to_python(
    yaml_path: str,
    python_path: str = "",
    tosca_dict: Optional[dict] = None,
    import_resolver=None,
    python_target_version=None,
    write_policy: WritePolicy = WritePolicy.auto,
    convert_repositories: bool = False,
) -> str:
    """
    Converts the given YAML service template to Python source code as a string and saves it to a file if ``python_path`` is provided.

    Args:
        yaml_path (str): Path to a YAML TOSCA service template
        python_path (str, optional): Location to save the converted Python source code. Defaults to "".
        tosca_dict (Optional[dict], optional): TOSCA service template as a ``dict``. Overrides ``yaml_path``. Defaults to None.
        import_resolver (optional): Import resolver to use. Defaults to None.
        python_target_version (int, optional): Minor version of Python 3 to target for code generation (Default: current version)

    Returns:
        str: The converted Python source code.
    """
    return convert_service_template(
        ToscaTemplate(
            path=yaml_path,
            yaml_dict_tpl=tosca_dict,
            import_resolver=import_resolver,
            verify=False,
        ),
        python_target_version,
        path=python_path,
        write_policy=write_policy,
        convert_repositories=convert_repositories,
    )


def convert_service_template(
    template: ToscaTemplate,
    python_compatible=None,
    builtin_prefix="",
    format=True,
    custom_defs=None,
    path="",
    write_policy: WritePolicy = WritePolicy.auto,
    base_dir=None,
    package_name="service_template",
    converted: Optional[Set[str]] = None,
    convert_repositories: bool = False,
) -> str:
    src = ""
    imports = Imports(bool(unfurl) and builtin_prefix != "tosca.")
    if not builtin_prefix:
        imports._set_builtin_imports()
        imports._set_ext_imports()
    if converted is None:
        converted = set()
    tpl = cast(Dict[str, Any], template.tpl)
    _tosca.global_state.mode = "parse"
    converter = Convert(
        template,
        True,
        python_compatible,
        builtin_prefix,
        imports,
        custom_defs,
        path,
        write_policy,
        base_dir,
        package_name,
    )
    imports_tpl = tpl.get("imports")
    if imports_tpl and isinstance(imports_tpl, list):
        for imp_def in imports_tpl:
            if isinstance(imp_def, str):
                imp_def = dict(file=imp_def)
            # base_dir is only set if imp_def has a repository
            (
                import_src,
                import_path,
                (module_name, ns),
                base_dir,
            ) = converter.convert_import(imp_def)
            loader.install(template.import_resolver)
            if not imp_def["file"].endswith(".py"):
                # if we aren't importing a python file, try to convert it to python
                if import_path not in converted:
                    converter.follow_import(
                        imp_def, import_path + ".py", format, base_dir, converted
                    )
                    converted.add(import_path)
            package = converter.get_package_name()
            try:
                module = importlib.import_module(module_name, package)
                imports.add_imports(ns, module.__dict__)
            except:
                if module_name[0] == ".":
                    logger.error(
                        f"error importing {module_name} in {package} {imp_def}",
                        exc_info=True,
                    )
                else:
                    logger.error(f"error importing {module_name}", exc_info=True)
            src += import_src
    imports_src = src

    metadata = tpl.get("metadata")
    if metadata:
        src += "tosca_metadata=" + value2python_repr(metadata, True) + "\n"

    template_tpl = template.tpl
    assert template_tpl
    if (
        convert_repositories
        or os.getenv("UNFURL_EXPORT_PYTHON_STYLE") == "include_repositories"
    ):
        repositories = template_tpl.get("repositories")
        if repositories:
            for name, value in repositories.items():
                src += converter.convert_repository(name, value)

    namespace: Dict[str, Any] = {}
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
                    imports.from_tosca.add("Namespace")
                    class_src = f"class {tosca_type}(Namespace):\n" + class_src
                converter.execute_source(imports_src + class_src, namespace)
                src += class_src + "\n"
    topology = template.topology_template
    if topology:
        src += converter.convert_topology(topology)
    deployment_blueprints = template_tpl.get("deployment_blueprints")
    if deployment_blueprints:
        imports.from_tosca.add("DeploymentBlueprint")
        for name, tpl in deployment_blueprints.items():
            src += converter.convert_blueprint(name, tpl)

    dsl_definitions = template_tpl.get("dsl_definitions")
    if dsl_definitions:
        src += "\ndsl_definitions=" + value2python_repr(dsl_definitions, True)

    prologue = write_policy.generate_comment("tosca.yaml2python", template.path or "")
    src = prologue + add_description(tpl, "") + imports.prelude() + src
    if not builtin_prefix and imports.get_all():
        src += f"\n__all__= {value2python_repr(imports.get_all(), True)}"
    # XXX fix relative imports and re-enable
    # src += '\nif __name__ == "__main__":\n    tosca.dump_yaml(globals())'

    if format:
        try:
            src = black.format_file_contents(src, fast=True, mode=black.mode.Mode())
        except black.report.NothingChanged:
            pass
        except Exception as e:
            logger.error("failed to format %s: %s", path, e)
    if path:
        overwrite, unchanged = write_policy.can_overwrite_compare(
            template.path, path, src
        )
        if overwrite and not unchanged:
            try:
                with open(path, "w") as po:
                    logger.info("writing to %s", path)
                    print(src, file=po)
            except Exception:
                logger.error("failed writing to %s", path)
        else:
            logger.info(
                "not writing to %s: %s", path, write_policy.deny_message(unchanged)
            )
    return src
