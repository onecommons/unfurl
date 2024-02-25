# Copyright (c) 2022 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Output a normalized json representation of a TOSCA service template for machine consumption.

* TOSCA datatypes are mapped to JSON Schema type definitions
* Node types are flattened to include their parent type's properties, capabilities, and requirements.
* Capabilities are directly represented, instead:
- Node types have a "extends" property that lists all the inherited types and capabilities associated with that type.
- Capability properties are represented as complex properties on the node type (and node template).
- Requirements has a "resourceType" which can be either a node type or a capability type.
"""
import re
import os
import os.path
import sys
import itertools
import json
import datetime
from time import perf_counter
import traceback
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    NewType,
    Optional,
    Tuple,
    cast,
)
from collections import Counter
from urllib.parse import urlparse
from toscaparser.elements.entity_type import Namespace
from toscaparser.imports import is_url, SourceInfo
from toscaparser.substitution_mappings import SubstitutionMappings
from toscaparser.properties import Property
from toscaparser.elements.constraints import Schema
from toscaparser.elements.property_definition import PropertyDef
from toscaparser.elements.nodetype import NodeType
from toscaparser.elements.statefulentitytype import StatefulEntityType
from toscaparser.entity_template import EntityTemplate
from toscaparser.common.exception import ExceptionCollector
from toscaparser.elements.scalarunit import get_scalarunit_class
from toscaparser.elements.datatype import DataType
from toscaparser.activities import ConditionClause
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.relationship_template import RelationshipTemplate
from .plan import _get_templates_from_topology
from .repo import sanitize_url
from .yamlmanifest import YamlManifest
from .logs import getLogger
from .spec import (
    EntitySpec,
    NodeSpec,
    TopologySpec,
    ToscaSpec,
    get_nodefilters,
)
from .util import UnfurlError, assert_not_none
from .localenv import LocalEnv, Project

logger = getLogger("unfurl")


from .graphql import (
    ApplicationBlueprint,
    Deployment,
    DeploymentEnvironment,
    DeploymentPath,
    DeploymentTemplate,
    GraphqlObject,
    DeploymentPaths,
    GraphqlObjectsByName,
    Requirement,
    ResourceTemplateName,
    ResourceTemplatesByName,
    ResourceType,
    ResourceTemplate,
    TypeName,
    ResourceTypesByName,
    GraphqlDB,
    RequirementConstraint,
    PropertyVisitor,
    ImportDef,
    get_import_def,
    is_property_user_visible,
    is_server_only_expression,
    _project_path,
    js_timestamp,
)

TypeDescendants = Dict[str, List[str]]


def _print(*args):
    print(*args, file=sys.stderr)


numeric_constraints = {
    "greater_than": "exclusiveMinimum",
    "greater_or_equal": "minimum",
    "less_than": "exclusiveMaximum",
    "less_or_equal": "maximum",
}


def map_constraint(jsonType, constraint, schema):
    key = constraint.constraint_key
    value = constraint.constraint_value
    unit = schema.get("default_unit")
    if unit and key != "in_range":
        scalar_class = get_scalarunit_class(schema["$toscatype"])
        # value has already been converted to the base unit, now we need to convert it back to the default_unit
        if scalar_class:
            value = scalar_class(
                str(value) + scalar_class.SCALAR_UNIT_DEFAULT
            ).get_num_from_scalar_unit(unit)
        else:
            logger.warning(
                "ignoring 'default_unit' metadata on property because type '%s' is not a scalar-unit",
                schema["$toscatype"],
            )
    if key == "schema":
        return value
    elif key == "pattern":
        return dict(pattern=value)
    elif key == "equal" or (key == "valid_values" and len(value) == 1):
        return dict(const=value)
    elif key == "valid_values":
        return dict(enum=value)
    elif key in numeric_constraints:
        return {numeric_constraints[key]: value}
    elif key in ["in_range", "length", "min_length", "max_length"]:
        suffix = dict(
            string="Length", object="Properties", array="Items", number="imum"
        )[jsonType]
        prefix = key[:3]
        if key == "in_range":
            start, stop = value
            if unit:
                # values have already been converted to the base unit, now we need to convert it back to the default_unit
                scalar_class = get_scalarunit_class(schema["$toscatype"])
                start = scalar_class(
                    str(start) + scalar_class.SCALAR_UNIT_DEFAULT
                ).get_num_from_scalar_unit(unit)
                stop = scalar_class(
                    str(stop) + scalar_class.SCALAR_UNIT_DEFAULT
                ).get_num_from_scalar_unit(unit)
            return {"min" + suffix: start, "max" + suffix: stop}
        if prefix == "min" or prefix == "max":
            return {prefix + suffix: value}
        elif key == "length":
            return {"min" + suffix: value, "max" + suffix: value}
        else:
            assert False


def map_constraints(jsonType, constraints, schema):
    if len(constraints) > 1:
        return dict(allOf=[map_constraint(jsonType, c, schema) for c in constraints])
    else:
        return map_constraint(jsonType, constraints[0], schema)


ONE_TO_ONE_TYPES = ("string", "boolean", "map", "list")

#  see constraints.PROPERTY_TYPES
VALUE_TYPES: Dict[str, Dict] = dict(
    integer={"type": "number"},
    string={"type": "string"},
    boolean={"type": "boolean"},
    float={"type": "number"},
    number={"type": "number"},
    timestamp={"type": "string"},
    map={"type": "object"},
    list={"type": "array"},
    version={"type": "string"},
    any={"type": "string"},  # XXX we'll have to interpret the string as json?
    range={"type": "array", "items": {"type": "integer", "maxItems": 2}},
    PortDef={"type": "number", "minimum": 1, "maximum": 65535},
    PortSpec={"type": "string"},
)

_SCALAR_TYPE: Any = {}
VALUE_TYPES.update(
    {
        "scalar-unit.size": _SCALAR_TYPE,
        "scalar-unit.frequency": _SCALAR_TYPE,
        "scalar-unit.time": _SCALAR_TYPE,
        "scalar-unit.bitrate": _SCALAR_TYPE,
        "tosca.datatypes.network.PortDef": VALUE_TYPES["PortDef"],
        "tosca.datatypes.network.PortSpec": VALUE_TYPES["PortSpec"],
    }
)


def tosca_type_to_jsonschema(custom_defs: Namespace, propdefs, toscatype):
    jsonschema = dict(
        type="object",
        properties={
            p.name: tosca_schema_to_jsonschema(p, custom_defs) for p in propdefs
        },
    )
    # add typeid:
    if toscatype:
        jsonschema["properties"].update({"$toscatype": dict(const=toscatype)})  # type: ignore
    return jsonschema


def _get_valuetype_schema(tosca_type, metadata):
    type_schema = VALUE_TYPES[tosca_type]
    if type_schema is _SCALAR_TYPE:
        return get_scalar_unit(tosca_type, metadata)
    else:
        return type_schema


def get_valuetype(dt, metadata):
    # find the base, built-in value type
    schema = Schema(None, dt.defs)
    while dt.value_type not in VALUE_TYPES:
        dt = dt.parent_type()
    return _get_valuetype_schema(dt.value_Type, metadata), schema


def get_simple_valuetype(tosca_type, custom_defs):
    if tosca_type in VALUE_TYPES:
        return tosca_type
    else:
        dt = DataType(tosca_type, custom_defs)
        if not dt.value_type:
            return None  # its a complex data type
        while dt.value_type not in VALUE_TYPES:
            dt = dt.parent_type()
        return dt.value_type


def get_scalar_unit(value_type, metadata):
    default_unit = metadata and metadata.get("default_unit")
    if default_unit:
        return {"type": "number", "default_unit": default_unit}
    else:
        return {"type": "string"}
    # XXX add format specifier
    # regex: "|".join(get_scalarunit_class(value_type).SCALAR_UNIT_DICT)


def _update_property_metadata(p: PropertyDef, metadata):
    property_metadata = metadata.get(p.name)
    if property_metadata:
        # don't modify original
        cp = PropertyDef(p.name, p.value, p.schema)
        if cp.schema:
            cp.schema.setdefault("metadata", {}).update(property_metadata)
        else:
            cp.schema = dict(type="string", metadata=property_metadata)
        return cp
    return p


def tosca_schema_to_jsonschema(p: PropertyDef, custom_defs: Namespace):
    # convert a PropertyDef to a property (this creates the Schema object we need)
    prop = Property(
        p.name,
        p.default,
        p.schema or dict(type="string"),
        custom_defs,
    )
    toscaSchema = prop.schema
    tosca_type = toscaSchema.type
    schema = {}
    if toscaSchema.title or p.name:
        schema["title"] = toscaSchema.title or p.name
    if toscaSchema.default is not None and not is_server_only_expression(
        toscaSchema.default
    ):
        schema["default"] = toscaSchema.default
    if toscaSchema.required:
        schema["required"] = True
    if toscaSchema.description:
        schema["description"] = toscaSchema.description

    metadata = toscaSchema.schema.get("metadata")
    if metadata:
        # set "sensitive" if present
        schema.update(metadata)
        property_metadata = metadata.get("property_metadata")
    else:
        property_metadata = None
    constraints = toscaSchema.constraints or []
    if tosca_type in VALUE_TYPES:
        type_schema = _get_valuetype_schema(tosca_type, metadata)
    else:
        dt = DataType(tosca_type, custom_defs)
        if dt.value_type:  # it's a simple type
            type_schema, valuetype_schema = get_valuetype(dt, metadata)
            if valuetype_schema.constraints:  # merge constraints
                constraints = valuetype_schema.constraints + constraints
        else:  # it's a complex type with properties:
            propdefs = []
            default_value = None
            if schema.get("default"):
                default_value = schema["default"].copy()
            for p in dt.get_properties_def_objects():
                if property_metadata:
                    p = _update_property_metadata(p, property_metadata)
                if is_property_user_visible(p):
                    propdefs.append(p)
                elif default_value:
                    default_value.pop(p.name, None)
            if default_value and default_value != schema["default"]:
                schema["default"] = default_value
            type_schema = tosca_type_to_jsonschema(custom_defs, propdefs, dt.type)
            metadata = dt.get_value("metadata", parent=True)
            if metadata and "additionalProperties" in metadata:
                schema["additionalProperties"] = dict(type="string", required=True)
                if (
                    len(type_schema["properties"]) == 1
                    and "$toscatype" in type_schema["properties"]
                ):
                    del type_schema["properties"]
    if tosca_type not in ONE_TO_ONE_TYPES and "properties" not in schema:
        schema["$toscatype"] = tosca_type
    schema.update(type_schema)

    if toscaSchema.entry_schema:
        entrySchema = tosca_schema_to_jsonschema(
            PropertyDef("", None, toscaSchema.entry_schema), custom_defs
        )
        if tosca_type == "list":
            schema["items"] = entrySchema
        else:
            schema["additionalProperties"] = entrySchema

    if constraints:
        schema.update(map_constraints(schema["type"], toscaSchema.constraints, schema))
    return schema


def _template_visibility(topology: TopologySpec, node_name: str, req_metadata: dict):
    entity_tpl = topology.get_node_src(node_name)
    if entity_tpl:
        node_metadata = entity_tpl.get("metadata") or {}
        if not node_metadata.get("user_settable") and not req_metadata.get(
            "user_settable"
        ):
            return "hidden"
        else:
            return "visible"
    return ""


def _requirement_visibility(topology: TopologySpec, name: str, req) -> str:
    if name in ["dependency", "installer"]:
        # skip artifact requirements and the base TOSCA relationship type that every node has
        return "omit"
    #  reqconstaint_from_nodetemplate() adds match
    node = req.get("match") or req.get("node")
    metadata = req.get("metadata") or {}
    if metadata.get("visibility"):
        return metadata["visibility"]
    if metadata.get("internal"):
        return "hidden"
    if node:
        visibility = _template_visibility(topology, node, metadata)
        if visibility:
            return visibility
    return "inherit"


def _get_req(req_dict) -> Tuple[str, dict]:
    name, req = list(req_dict.items())[0]
    if isinstance(req, str):
        req = dict(node=req)
    else:
        assert isinstance(req, dict), f"bad {req} in {req_dict}"
    return name, req


def find_descendents(descendents: TypeDescendants, typename: str) -> Iterator[str]:
    if typename in descendents:
        for child in descendents[typename]:
            yield child
            yield from find_descendents(descendents, child)


def _find_req_typename(
    types: ResourceTypesByName,
    typename: TypeName,
    reqname: str,
    topology: TopologySpec,
    descendents: Optional[TypeDescendants],
) -> TypeName:
    # find the name of the resource type that is the target of "reqname" on "typename"
    # have types[typename] go first
    for target_reqs in types[typename]["requirements"]:
        if target_reqs["name"] == reqname:
            return target_reqs["resourceType"]

    # reqname wasn't found, look for it on subtypes of typename
    if not descendents:
        return TypeName("")

    matches = []
    for subtype in find_descendents(descendents, typename):
        t = types.get_type(subtype)
        if not t:
            t = _node_typename_to_graphql(subtype, topology, types)
            if not t:
                continue
        if "requirements" not in t or t.get("metadata", {}).get("alias"):
            continue
        assert typename in t["extends"], f"{typename} not in {t['extends']}"
        # type is typename or derived from typename
        for target_reqs in t["requirements"]:
            if target_reqs["name"] == reqname:
                match = target_reqs["resourceType"]
                matches.append(match)

    # we want a leaf type, so choose the last one
    return matches[-1] if matches else TypeName("")


def _make_req(
    req_dict: dict,
    topology: Optional[TopologySpec] = None,
    types: Optional[ResourceTypesByName] = None,
    typename: Optional[TypeName] = None,
    descendents: Optional[TypeDescendants] = None,
) -> Tuple[str, dict, RequirementConstraint]:
    name, req = _get_req(req_dict)
    reqobj = RequirementConstraint(  # type: ignore
        name=name,
        description=req.get("description") or "",
        __typename="RequirementConstraint",
    )
    metadata = req.get("metadata")

    if metadata:
        if "badge" in metadata:
            reqobj["badge"] = metadata["badge"]
        if "title" in metadata:
            reqobj["title"] = metadata["title"]
        if "icon" in metadata:
            reqobj["icon"] = metadata["icon"]

    if "occurrences" in req:
        reqobj["min"] = req["occurrences"][0]
        reqobj["max"] = req["occurrences"][1]
        if reqobj["max"] == "UNBOUNDED":
            reqobj["max"] = 0xFFFFFFFF

    if req.get("node_filter"):
        reqobj["node_filter"] = req["node_filter"]
        if types is not None:
            assert topology
            assert typename is not None
            # we're called from annotate_requirements annotating a nested requirements constraint
            reqtypename = _find_req_typename(
                types, typename, name, topology, descendents
            )
            if reqtypename:
                _annotate_requirement(reqobj, reqtypename, topology, types, descendents)
            else:
                logger.warning(
                    f'could not find target resource type of requirement "{name}" on "{typename}"'
                )
                reqobj.pop("node_filter")
    return name, req, reqobj


def requirement_to_graphql(
    topology: TopologySpec,
    req_dict: dict,
    types: ResourceTypesByName,
    include_omitted=False,
    include_matches: bool = True,
    type_definition: Optional[NodeType] = None,
) -> Optional[RequirementConstraint]:
    name, req, reqobj = _make_req(req_dict)
    if "min" not in reqobj:
        # set defaults
        reqobj["min"] = 1  # type: ignore  # unreachable
        reqobj["max"] = 1

    visibility = _requirement_visibility(topology, name, req)
    if not include_omitted and visibility == "omit":
        return None

    if visibility and visibility != "inherit":
        reqobj["visibility"] = visibility

    if reqobj["max"] == 0:
        return None

    reqobj["match"] = None
    nodetype = req.get("node")
    namespace_id = req.get("!namespace-node")
    if nodetype:
        # req['node'] can be a node_template name instead of a type
        node_template = topology.topology_template.find_node_related_template(nodetype, namespace_id)
        if node_template:
            if include_matches:
                reqobj["match"] = node_template.name
            nodetype = node_template.type
    else:
        nodetype = req.get("capability")
        if not nodetype:
            rel = req.get("relationship")
            if rel:
                if isinstance(rel, str):
                    if rel in topology.relationship_templates:
                        relspec = topology.relationship_templates[rel]
                        if relspec.target:
                            reqobj["match"] = relspec.target.name
                            nodetype = relspec.target.type
                        else:
                            target_types = (
                                relspec.toscaEntityTemplate.type_definition.valid_target_types
                            )
                            if target_types:
                                nodetype = target_types[0]
                    else:
                        return None  # XXX else rel is a typename, look up type and find its target_types
                else:
                    return None  # XXX else rel is an inline relationship template, look up type and find its target_types
    if not nodetype:
        logger.warning(
            "skipping requirement constraint %s, there was no type specified ", req_dict
        )
        return None
    if type_definition and type_definition.custom_def:
        reqobj["resourceType"] = TypeName(
            type_definition.custom_def.get_global_name(nodetype)
        )
    else:
        reqobj["resourceType"] = types.expand_typename(nodetype)
    return reqobj


def template_visibility(t: EntitySpec, discovered):
    metadata = t.toscaEntityTemplate.entity_tpl.get("metadata")
    if metadata:
        if metadata.get("visibility"):
            return metadata["visibility"]
        if metadata.get("internal"):
            return "hidden"

    if t.type in ["unfurl.nodes.ArtifactInstaller", "unfurl.nodes.LocalRepository"]:
        return "omit"  # skip artifacts
    if "default" in t.directives:
        return "hidden"
    if discovered and t.nested_name in discovered:
        return "omit"
    return "inherit"


def add_root_source_info(jsontype: ResourceType, repo_url: str, base_path: str) -> None:
    # if the type wasn't imported add source info pointing at the root service template
    source_info = cast(ImportDef, jsontype.get("_sourceinfo"))
    if not source_info:
        if jsontype["name"] in StatefulEntityType.TOSCA_DEF:
            return  # built-in type
        # not an import, type defined in main service template file
        # or it's an import relative to the root, just include the root import because it will in turn import this import
        jsontype["_sourceinfo"] = dict(
            url=sanitize_url(repo_url, False), file=base_path
        )
    elif "url" not in source_info:
        # it's an import relative to the root, set to the repo's url
        source_info["url"] = sanitize_url(repo_url, False)


# XXX outputs: only include "public" attributes?
def node_type_to_graphql(
    topology: TopologySpec,
    type_definition: StatefulEntityType,
    types: ResourceTypesByName,
    summary: bool = False,
    include_matches: bool = True,
) -> Optional[ResourceType]:
    custom_defs = type_definition.custom_def
    assert isinstance(custom_defs, Namespace)
    raw_type = type_definition.type
    typename = types.get_typename(type_definition)
    jsontype = ResourceType(
        name=typename,  # fully qualified name
        title=type_definition.type.split(".")[-1],  # short, readable name
        description=type_definition.get_value("description", "") or "",
        __typename="ResourceType",
        extends=[],
        requirements=[],
        inputsSchema={},
    )
    types[typename] = jsontype  # set now to avoid circular reference via _get_extends
    metadata = type_definition.get_value("metadata")
    inherited_metadata = type_definition.get_value("metadata", parent=True)
    visibility = "inherit"
    if inherited_metadata:
        if "badge" in inherited_metadata:
            jsontype["badge"] = inherited_metadata["badge"]
        if "icon" in inherited_metadata:
            jsontype["icon"] = inherited_metadata["icon"]
        if "details_url" in inherited_metadata:
            jsontype["details_url"] = inherited_metadata["details_url"]
    if metadata:
        # we don't want the inherited values for these
        if "title" in metadata:
            jsontype["title"] = metadata["title"]
        if metadata.get("internal"):
            visibility = "hidden"
        jsontype["metadata"] = metadata

    _source = type_definition.get_value("_source")
    if isinstance(_source, dict):  # could be str or None
        jsontype["_sourceinfo"] = get_import_def(cast(SourceInfo, _source))
        prefix = custom_defs.find_prefix(raw_type)
        if prefix:
            jsontype["_sourceinfo"]["prefix"] = prefix  # type: ignore

    if type_definition.defs is None:
        logger.warning("%s is missing type definition", type_definition.type)
        jsontype["extends"] = []
        del types[typename]
        return None

    extends: List[TypeName] = []
    # add ancestors classes to extends
    types._get_extends(topology, type_definition, extends, node_type_to_graphql)
    if isinstance(type_definition, NodeType):
        # add capabilities types to extends
        for cap in type_definition.get_capability_typedefs():
            types._get_extends(topology, cap, extends, None)
    jsontype["extends"] = extends

    operations = set(
        op.name
        for op in EntityTemplate._create_interfaces(type_definition, None)
        if op.interfacetype != "Mock"
    )
    jsontype["implementations"] = sorted(operations)
    # XXX relationship type might not be in types
    jsontype["implementation_requirements"] = [
        types.expand_typename(ir) for ir in type_definition.get_interface_requirements()
    ]

    if summary:
        return jsontype

    jsontype["visibility"] = visibility

    propertydefs = list(
        (p, is_property_user_visible(p))
        for p in type_definition.get_properties_def_objects()
    )
    jsontype["inputsSchema"] = tosca_type_to_jsonschema(
        custom_defs, (p[0] for p in propertydefs if p[1]), None
    )
    jsontype["computedPropertiesSchema"] = tosca_type_to_jsonschema(
        custom_defs, (p[0] for p in propertydefs if not p[1]), None
    )

    if not isinstance(type_definition, NodeType):
        return jsontype

    # treat each capability as a complex property
    add_capabilities_as_properties(
        jsontype["inputsSchema"]["properties"], type_definition, custom_defs
    )
    # XXX only include "public" attributes?
    attributedefs = (
        p
        for p in type_definition.get_attributes_def_objects()
        if is_property_user_visible(p)
    )
    jsontype["outputsSchema"] = tosca_type_to_jsonschema(
        custom_defs, attributedefs, None
    )
    # XXX capabilities can hava attributes too
    # add_capabilities_as_attributes(jsontype["outputs"], type_definition, custom_defs)

    jsontype["requirements"] = list(
        filter(
            None,
            [
                requirement_to_graphql(
                    topology,
                    req,
                    types,
                    include_matches=include_matches,
                    type_definition=type_definition,
                )
                for req in type_definition.get_all_requirements()
            ],
        )
    )
    return jsontype


def _update_root_type(
    jsontype: ResourceType,
    sub_map: SubstitutionMappings,
    topology: TopologySpec,
    types: ResourceTypesByName,
):
    "Modify a type representation so that it can be used with template with a nested topology."

    # don't display any properties set by the inner topology
    for name, value in sub_map.get_declared_properties().items():
        inputsSchema = jsontype["inputsSchema"]["properties"].get(name)
        if inputsSchema:
            if is_server_only_expression(value):
                del jsontype["inputsSchema"]["properties"][name]
            else:
                inputsSchema["default"] = value
        else:
            jsontype["computedPropertiesSchema"]["properties"].pop(name, None)

    # make optional any requirements that were set in the inner topology
    # or have a predefined match (since it would hidden be in the nested topology)
    names = sub_map.get_declared_requirement_names()
    for req in jsontype.setdefault("requirements", []):
        if req["name"] in names or req["match"]:
            req["min"] = 0
            req["match"] = None
    # templates created with this type need to have the substitute directive
    jsontype["directives"] = ["substitute"]
    # find all the default nodes that are being referenced directly or indirectly
    # this will find the required nodes
    list(_get_templates_from_topology(topology, set(), None))
    placeholders = [
        node
        for node in topology.node_templates.values()
        if (
            "default" in node.directives
            or node.name in topology.spec.overridden_default_templates
        )
        and node.type != "unfurl.nodes.LocalRepository"  # exclude reified artifacts
        and node.required
    ]
    # add those as requirement on the root type
    for node in placeholders:
        # XXX copy node_filter and metadata from get_relationship_templates()
        placeholder_req = {node.name: dict(node=node.type)}
        req_json = requirement_to_graphql(
            topology,
            placeholder_req,
            types,
            True,
            include_matches=False,
            type_definition=node.toscaEntityTemplate.type_definition,  # type: ignore
        )
        if req_json:
            jsontype["requirements"].append(req_json)  # type: ignore


def to_graphql_nodetypes(
    spec: ToscaSpec, include_all: bool, types: ResourceTypesByName
) -> ResourceTypesByName:
    # node types are readonly, so mapping doesn't need to be bijective
    topology = spec.topology
    assert topology
    ExceptionCollector.collecting = True
    # create these ones first
    # XXX detect error later if these types are being used elsewhere
    for nested_topology in spec.nested_topologies:
        sub_map = nested_topology.topology_template.substitution_mappings
        if sub_map:
            typedef = sub_map.node_type
            if typedef:
                jsontype = node_type_to_graphql(nested_topology, typedef, types)
                if jsontype:
                    _update_root_type(jsontype, sub_map, nested_topology, types)

    descendents: TypeDescendants = {}
    for typename, defs in types.custom_defs.items():
        typename, prefix = types.custom_defs.get_global_name_and_prefix(typename)
        parents = defs.get("derived_from", [])
        if isinstance(parents, str):
            parents = (parents,)
        for parent in parents:
            if prefix:
                parent = f"{prefix}.{parent}"
            parent = types.custom_defs.get_global_name(parent)
            descendents.setdefault(parent, []).append(typename)
        if defs.get("metadata"):
            aliases = defs["metadata"].get("deprecates")
            if aliases:
                if isinstance(aliases, str):
                    aliases = (aliases,)
                for alias in aliases:
                    descendents.setdefault(alias, []).append(typename)
        if not include_all or types.get_type(typename):
            continue
        typedef = types._make_typedef(typename)
        if typedef:
            node_type_to_graphql(topology, typedef, types)

    if include_all:
        for typename in cast(str, StatefulEntityType.TOSCA_DEF):  # builtin types
            if typename.startswith("unfurl.nodes") or typename.startswith(
                "unfurl.relationships"
            ):
                if types.get_type(typename):
                    continue
                # only include our extensions
                typedef = types._make_typedef(typename)
                if typedef:
                    node_type_to_graphql(topology, typedef, types)

    for node_spec in topology.node_templates.values():
        type_definition = node_spec.toscaEntityTemplate.type_definition
        assert type_definition
        if not types.get_type(type_definition.global_name):
            node_type_to_graphql(topology, type_definition, types)

    assert spec.topology
    annotate_requirements(spec.topology, types, descendents)
    mark_leaf_types(types)
    return types


# currently unused:
# def deduce_valuetype(value):
#     typemap = {
#         int: "number",
#         float: "number",
#         type(None): "null",
#         dict: "object",
#         list: "array",
#     }
#     for pythontype in typemap:
#         if isinstance(value, pythontype):
#             return dict(type=typemap[pythontype])
#     return {}


def add_capabilities_as_properties(schema, nodetype, custom_defs: Namespace):
    """treat each capability as a property and add them to props"""
    for cap in nodetype.get_capability_typedefs():
        if not cap.get_definition("properties"):
            continue
        propdefs = [
            p for p in cap.get_properties_def_objects() if is_property_user_visible(p)
        ]
        schema[cap.name] = tosca_type_to_jsonschema(custom_defs, propdefs, cap.type)


def add_capabilities_as_attributes(schema, nodetype, custom_defs: Namespace):
    """treat each capability as an attribute and add them to props"""
    for cap in nodetype.get_capability_typedefs():
        if not cap.get_definition("attributes"):
            continue
        propdefs = [
            p for p in cap.get_attributes_def_objects() if is_property_user_visible(p)
        ]
        schema[cap.name] = tosca_type_to_jsonschema(custom_defs, propdefs, cap.type)


def _get_typedef(name: str, custom_defs: Namespace) -> Optional[Dict[str, Any]]:
    typedef = custom_defs.get(name)
    if not typedef:
        typedef = StatefulEntityType.TOSCA_DEF.get(name)
    return typedef


def find_reqconstraint_for_template(
    nodespec: NodeSpec, name: str, types: ResourceTypesByName
) -> Optional[RequirementConstraint]:
    typeobj = types.get_type(nodespec.global_type)
    if not typeobj:
        typeobj = _node_typename_to_graphql(nodespec.type, nodespec.topology, types)
        if not typeobj:
            return None
    for constraint in typeobj["requirements"]:
        if name == constraint["name"]:
            return constraint
    return None


def create_requirement_for_template(
    nodespec: NodeSpec,
    name: str,
    reqconstraint: RequirementConstraint,
    _match: Optional[str],
    metadata: Dict[str, Any],
) -> Optional[Requirement]:
    visibility = metadata.get("visibility")
    if _match and not _get_typedef(
        _match, nodespec.topology.topology_template.custom_defs
    ):
        # it's not a type, assume it's a node template
        # (the node template name might not match a template if it is only defined in the deployment blueprints)
        match: Optional[str] = _match
        if not visibility:
            visibility = _template_visibility(nodespec.topology, _match, metadata)
    else:
        # use the match set in the type definition
        match = reqconstraint.get("match")
    visibility = visibility or reqconstraint.get("visibility")
    reqjson = Requirement(
        constraint=reqconstraint, name=name, match=match, __typename="Requirement"
    )
    if visibility:
        reqjson["visibility"] = visibility
    return reqjson


def create_reqconstraint_from_nodetemplate(
    nodespec: EntitySpec, name: str, req_dict: dict, types: ResourceTypesByName
):
    # we need to call _get_explicit_relationship() to make sure all the requirements relationships are created
    # _get_explicit_relationship returns req_dict merged with its type definition
    req_dict, rel_template = cast(
        NodeTemplate, nodespec.toscaEntityTemplate
    )._get_explicit_relationship(name, req_dict)
    if "constraint" in req_dict.get("metadata", {}):
        # this happens when we import graphql json directly
        reqconstraint = req_dict["metadata"]["constraint"]
    else:
        # "node" on a node template's requirement in TOSCA yaml is the node match so don't use as part of the constraint
        # use the type's "node" (unless the requirement isn't defined on the type at all)
        type_definition = cast(NodeType, nodespec.toscaEntityTemplate.type_definition)
        typeReqDef = type_definition.get_requirement_definition(name)
        if typeReqDef:
            # preserve node as "match" for _requirement_visibility
            req_dict["match"] = req_dict.get("node")
            if "node" in typeReqDef:
                req_dict["node"] = typeReqDef["node"]
            elif "capability" in req_dict:
                req_dict.pop("node", None)
            # else: empty typeReqDef, leave req_dict alone
        reqconstraint = requirement_to_graphql(
            nodespec.topology, {name: req_dict}, types, type_definition=type_definition
        )

    if reqconstraint:
        # XXX do we still need this?
        # if "substitute" in typeobj.get("directives", [])
        #   for typeconstraint in typeobj["requirements"]:
        #     if name == typeconstraint["name"]:
        #         # then we've might have modified the type in _update_root_type()
        #         # and the tosca object won't know about that change so set it now
        #         if not req_dict.get("node"):
        #             reqconstraint["match"] = typeconstraint.get("match")
        #             match = None
        #         reqconstraint["min"] = typeconstraint["min"]
        #         if "visibility" in typeconstraint:
        #             reqconstraint["visibility"] = typeconstraint["visibility"]
        _annotate_requirement(
            reqconstraint, reqconstraint["resourceType"], nodespec.topology, types, None
        )
    return reqconstraint


def _find_typename(
    nodetemplate: EntityTemplate,
    types: ResourceTypesByName,
) -> TypeName:
    # XXX
    # if we substituting template, generate a new type
    # json type for root of imported blueprint only include unfulfilled requirements
    # and set defaults on properties set by the inner root
    return types.get_typename(nodetemplate.type_definition)


def nodetemplate_to_json(
    node_spec: EntitySpec,
    types: ResourceTypesByName,
    for_resource: bool = False,
) -> ResourceTemplate:
    """
    Returns json object as a ResourceTemplate:
    """
    nodetemplate = node_spec.toscaEntityTemplate
    spec = node_spec.spec
    if "__typename" in nodetemplate.entity_tpl:
        # previously imported from the json, just return it
        ogprops = nodetemplate.entity_tpl.pop("_original_properties", None)
        if ogprops is not None:
            nodetemplate.entity_tpl["properties"] = ogprops
        return nodetemplate.entity_tpl

    metadata = nodetemplate.entity_tpl.get("metadata", {}).copy()
    title = metadata.pop("title", None)
    json = ResourceTemplate(  # type: ignore
        type=_find_typename(nodetemplate, types),
        name=nodetemplate.name,
        title=title or nodetemplate.name,
        description=nodetemplate.entity_tpl.get("description") or "",
        directives=nodetemplate.directives,
        # __typename="ResourceTemplate",  # XXX
    )
    if "imported" in nodetemplate.entity_tpl:
        json["imported"] = nodetemplate.entity_tpl.get("imported")
    if metadata:
        metadata.pop("before_patch", None)
        json["metadata"] = metadata

    visitor = PropertyVisitor()
    json["properties"] = list(visitor.template_properties_to_json(nodetemplate))
    # if visitor.redacted and "predefined" not in nodetemplate.directives:
    #     json["directives"].append("predefined")
    #     logger.warning(
    #         "Adding 'predefined' directive to '%s' because it has redacted properties",
    #         nodetemplate.name,
    #     )
    json["dependencies"] = []
    discovered = None if not for_resource else spec.discovered
    visibility = template_visibility(node_spec, discovered)
    if visibility != "inherit":
        json["visibility"] = visibility
        logger.debug(f"setting visibility {visibility} on template {nodetemplate.name}")

    if not isinstance(nodetemplate.type_definition, NodeType):
        return json

    # treat each capability as a complex property
    capabilities = nodetemplate.get_capabilities()
    for prop in json["properties"]:
        # properties are already added via the type definition
        cap = capabilities.get(prop["name"])
        if cap:
            # XXX need to map property values like property_value_to_json above
            prop["value"] = cap._properties

    # XXX
    # this is the same as on the type because of the bug where attribute set on a node_template are ignored
    # json["outputs"] = jsonnodetype["outputs"]

    has_visible_dependency = False
    ExceptionCollector.start()
    nodetemplate = cast(NodeTemplate, nodetemplate)
    node_spec = cast(NodeSpec, node_spec)
    for name, req_dict in nodetemplate.all_requirements:
        if not req_dict:
            # not defined on the template at all
            reqconstraint = find_reqconstraint_for_template(node_spec, name, types)
            match = None
            metadata = {}
        else:
            req_dict_copy = req_dict.copy()
            match = req_dict_copy.pop("node", None)
            metadata = req_dict_copy.pop("metadata", {})
            if (
                req_dict_copy
                or name
                not in cast(
                    NodeType, node_spec.toscaEntityTemplate.type_definition
                ).requirement_definitions
            ):
                reqconstraint = create_reqconstraint_from_nodetemplate(
                    node_spec, name, req_dict, types
                )
            else:
                reqconstraint = find_reqconstraint_for_template(node_spec, name, types)
        if reqconstraint is None:
            continue
        reqjson = create_requirement_for_template(
            node_spec, name, reqconstraint, match, metadata
        )
        if reqjson is None:
            continue
        if reqjson.get("visibility") != "hidden":
            has_visible_dependency = True
        json["dependencies"].append(reqjson)

    if (
        not for_resource
        and "predefined" in json["directives"]
        and json.get("visibility") not in ["hidden", "omit"]
    ):
        if visitor.user_settable:
            raise UnfurlError(
                f"Can't export template '{nodetemplate.name}' because it is both predefined and has settings the user can change."
            )
        if has_visible_dependency:
            raise UnfurlError(
                f"Can't export template '{nodetemplate.name}' because it is both predefined and has requirements the user can change."
            )
    return json


primary_name = "__primary"


def _generate_primary(
    spec: ToscaSpec, db: GraphqlDB, node_tpl=None, requirements=None
) -> NodeTemplate:
    base_type = node_tpl["type"] if node_tpl else "tosca.nodes.Root"
    topology = spec.template.topology_template
    assert topology
    # generate a node type and node template that represents root of the topology
    # generate a type that exposes the topology's inputs and outputs as properties and attributes
    attributes = {  # XXX is this computed?
        o.name: dict(default=o.value, type="string", description=o.description or "")
        for o in topology.outputs
    }
    nodetype_tpl = dict(
        derived_from=base_type, properties=topology._tpl_inputs(), attributes=attributes
    )
    # set as requirements all the node templates that aren't the target of any other requirements
    assert spec.topology
    if requirements is None:
        roots = [
            node.toscaEntityTemplate
            for node in spec.topology.node_templates.values()
            if not node.toscaEntityTemplate.get_relationship_templates()
            and "default" not in node.directives
        ]
        requirements = [{node.name: dict(node=node.type)} for node in roots]
    else:
        roots = None
    nodetype_tpl["requirements"] = requirements
    topology.custom_defs[primary_name] = nodetype_tpl
    tpl = node_tpl or {}
    tpl["type"] = primary_name
    tpl.setdefault("properties", {}).update(
        {name: dict(get_input=name) for name in topology._tpl_inputs()}
    )
    # if create new template, need to assign the nodes explicitly (needed if multiple templates have the same type)
    if not node_tpl and roots:
        tpl["requirements"] = [{node.name: dict(node=node.name)} for node in roots]
    node_spec = spec.topology.add_node_template(primary_name, tpl, False)
    node_template = cast(NodeTemplate, node_spec.toscaEntityTemplate)

    types = db.get_types()
    assert node_template.type_definition
    node_type_to_graphql(spec.topology, node_template.type_definition, types)
    db["ResourceTemplate"][node_template.name] = nodetemplate_to_json(node_spec, types)
    return node_template


# if a node type or template is specified, use that, but it needs to be compatible with the generated type
def _get_or_make_primary(
    spec: ToscaSpec, db: GraphqlDB, nested: bool
) -> Tuple[str, TypeName]:
    ExceptionCollector.start()  # topology.add_template may generate validation exceptions
    assert spec.template
    topology = spec.template.topology_template
    assert topology
    # we need to generate a root template
    root_type: Optional[NodeType] = None
    root = None
    if topology.substitution_mappings:
        root_type = topology.substitution_mappings.node_type
        # XXX different namespace
        root = topology.substitution_mappings._node_template
    types = db.get_types()
    if root_type:
        properties_tpl = root_type.get_definition("properties") or {}
        if nested:
            assert spec.topology
            jsontype = types.get_type(root_type.type)
            if not jsontype:
                jsontype = node_type_to_graphql(spec.topology, root_type, types)
            assert topology.substitution_mappings
            if jsontype:
                _update_root_type(
                    jsontype, topology.substitution_mappings, spec.topology, types
                )
        # if no property mapping in use, generate a new root template if there are any missing inputs
        elif (
            not topology.substitution_mappings
            or not topology.substitution_mappings.has_property_mapping()
        ):
            for input in topology.inputs:
                if input.name not in properties_tpl:
                    # there's an input that isn't handled by the type, generate a new one
                    root = _generate_primary(spec, db, root and root.entity_tpl)
                    break
        if not root:
            root = topology.node_templates.get(primary_name)
            if not root:
                properties = {
                    name: dict(get_input=name) for name in topology._tpl_inputs()
                }
                tpl = dict(type=root_type.type, properties=properties)
                assert spec.topology
                node_spec = spec.topology.add_node_template(primary_name, tpl, False)
                root = node_spec.toscaEntityTemplate
                # XXX connections are missing
                db["ResourceTemplate"][root.name] = nodetemplate_to_json(
                    node_spec, db.get_types()
                )
    else:
        root = _generate_primary(spec, db)

    assert root and root.type
    return root.name, types.get_typename(root.type_definition)


def blueprint_metadata(spec: ToscaSpec, root_name: str) -> ApplicationBlueprint:
    title, name = _template_title(spec, root_name)
    blueprint = ApplicationBlueprint(
        name=name, __typename="ApplicationBlueprint", title=title
    )
    blueprint["description"] = spec.template.description or ""
    metadata = spec.template.tpl.get("metadata") or {}
    blueprint["livePreview"] = metadata.get("livePreview")
    blueprint["sourceCodeUrl"] = metadata.get("sourceCodeUrl")
    blueprint["image"] = metadata.get("image")
    blueprint["projectIcon"] = metadata.get("projectIcon")
    blueprint["primaryDeploymentBlueprint"] = metadata.get("primaryDeploymentBlueprint")
    return blueprint


def to_graphql_blueprint(
    spec: ToscaSpec, db: GraphqlDB, nested=False
) -> Tuple[ApplicationBlueprint, str]:
    # note: root_resource_template is derived from inputs, outputs and substitution_template from topology_template
    root_name, root_type = _get_or_make_primary(spec, db, nested)
    blueprint = blueprint_metadata(spec, root_name)
    blueprint["primary"] = root_type
    blueprint["deploymentTemplates"] = []
    return blueprint, root_name


def slugify(text):
    # XXX only allow env var names?
    text = text.lower().strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w\-]", "", text)
    return re.sub(r"\-\-+", "-", text)


def _template_title(spec, default):
    metadata = spec.template.tpl.get("metadata") or spec.template.tpl
    name = metadata.get("template_name") or default
    slug = slugify(name)
    title = metadata.get("title") or name
    return title, slug


def _generate_env_names_from_type(reqname, type_definition, custom_defs):
    for propdef in itertools.chain(
        type_definition.get_properties_def_objects(),
        type_definition.get_attributes_def_objects(),
    ):
        if propdef.name in ["tosca_id", "state", "tosca_name"]:
            continue
        simple_type = get_simple_valuetype(propdef.schema["type"], custom_defs)
        if simple_type and VALUE_TYPES[simple_type].get("type") in [
            "string",
            "boolean",
            "number",
        ]:
            yield f"{reqname}_{propdef.name.upper()}"


def _generate_env_names(
    spec: ToscaSpec, root_name: str, types: ResourceTypesByName
) -> Iterator[str]:
    assert spec.topology
    primary = spec.topology.get_node_template(root_name)
    assert primary
    primary_type = primary.toscaEntityTemplate.type_definition
    assert primary_type
    yield from _generate_env_names_from_type("APP", primary_type, types.custom_defs)
    for req in primary_type.get_all_requirements():
        # get the target type
        req_json = requirement_to_graphql(spec.topology, req, types)
        if not req_json:
            continue
        name = req_json["name"]
        target_type = cast(TypeName, req_json["resourceType"])
        typedef = types._make_typedef(target_type)
        if typedef:
            yield from _generate_env_names_from_type(
                name.upper(), typedef, types.custom_defs
            )
        else:
            logger.warning(
                f"unable to generate enviroment variable names for type {target_type}: could not find its type definition."
            )


def get_deployment_blueprints(
    manifest: YamlManifest, blueprint: GraphqlObject, root_name: str, db: GraphqlDB
) -> Dict[str, DeploymentTemplate]:
    """
    The blueprint will include deployement blueprints that follows this syntax in the manifest:

    .. code-block:: YAML

      spec:
        deployment_blueprints:
          <name>:
            title: Google Cloud Platform
            cloud: unfurl.relationships.ConnectsTo.GoogleCloudProject
            description: Deploy compute instances on Google Cloud Platform
            resource_templates:
              <map of ResourceTemplates>
            # graphql json import / export only:
            resourceTemplates: [resource_template_name*]

    Returns json object with DeploymentTemplates:
    """
    deployment_blueprints: Dict[str, dict] = (
        manifest.manifest.expanded.get("spec", {}).get("deployment_blueprints") or {}
    )
    spec = manifest.tosca
    assert spec and spec.topology
    deployment_templates: Dict[str, DeploymentTemplate] = {}
    for name, tpl in deployment_blueprints.items():
        slug = slugify(name)
        template = tpl.copy()
        local_resource_templates: ResourceTemplatesByName = {}
        resource_templates = template.pop("resource_templates", None)
        if resource_templates:
            topology = spec.topology.copy()
            for node_name, node_tpl in resource_templates.items():
                # nodes here overrides node_templates
                node_spec = topology.add_node_template(node_name, node_tpl, False)
            # convert to json in second-pass template requirements can reference each other
            for node_name, node_tpl in resource_templates.items():
                node_spec = topology.get_node_template(node_name)
                assert node_spec
                t = nodetemplate_to_json(node_spec, db.get_types())
                if t.get("visibility") == "omit":
                    continue
                local_resource_templates[node_name] = t

        # XXX assert that db["ResourceTemplate"] has already has all the node_templates
        # names of ResourceTemplates:
        resourceTemplates = cast(
            List[ResourceTemplateName],
            sorted(set(db["ResourceTemplate"]) | set(local_resource_templates)),
        )
        primary: str = tpl.get("primary") or root_name
        env_vars = list(_generate_env_names(spec, primary, db.get_types()))
        dt = DeploymentTemplate(
            __typename="DeploymentTemplate",
            title=cast(str, tpl.get("title") or name),
            description=cast(str, tpl.get("description", "")),
            name=name,
            slug=slug,
            blueprint=blueprint["name"],
            primary=primary,
            resourceTemplates=resourceTemplates,
            ResourceTemplate=local_resource_templates,
            environmentVariableNames=env_vars,
        )
        template.update(dt)
        deployment_templates[name] = cast(DeploymentTemplate, template)
    return deployment_templates


def get_blueprint_from_topology(
    manifest: YamlManifest, db: GraphqlDB
) -> Tuple[ApplicationBlueprint, DeploymentTemplate]:
    spec = manifest.tosca
    assert spec
    # XXX cloud = spec.topology.primary_provider
    blueprint, root_name = to_graphql_blueprint(spec, db)
    templates = get_deployment_blueprints(manifest, blueprint, root_name, db)

    deployment_blueprint_name = manifest.context.get("deployment_blueprint")
    if deployment_blueprint_name and deployment_blueprint_name in templates:
        deployment_blueprint = templates[deployment_blueprint_name]
        template = deployment_blueprint.copy()
    else:
        template = DeploymentTemplate()  # type: ignore

    # the deployment template created for this deployment will have a "source" key
    # so if it doesn't (or one wasn't set) create a new one and set the current one as its "source"
    if "source" not in template:
        title = (
            os.path.basename(os.path.dirname(manifest.path))
            if manifest.path
            else "untitled"
        )
        slug = slugify(title)
        dt = DeploymentTemplate(
            __typename="DeploymentTemplate",
            title=title,
            name=slug,
            slug=slug,
            description=spec.template.description or "",
            # names of ResourceTemplates
            resourceTemplates=sorted(db["ResourceTemplate"]),
            ResourceTemplate={},
            source=deployment_blueprint_name,
            projectPath=_project_path(manifest.repositories["spec"].url),
        )
        template.update(dt)  # type: ignore
        templates[slug] = template

    blueprint["deploymentTemplates"] = list(templates)
    template["blueprint"] = blueprint["name"]
    template["primary"] = root_name
    template["environmentVariableNames"] = list(
        _generate_env_names(spec, root_name, db.get_types())
    )
    last_commit_time = manifest.last_commit_time()
    if last_commit_time:
        template["commitTime"] = js_timestamp(last_commit_time)
    return blueprint, template


def _add_repositories(db: dict, tpl: dict):
    imports_tpl = tpl.get("imports")
    repositories = {}
    repositories_tpl = tpl.get("repositories") or {}
    # only export well-known type repositories, avoid built-in repositories
    for type_repos in ("types", "std"):
        types_repo = repositories_tpl.get(type_repos)
        if types_repo:
            repositories[type_repos] = types_repo
            repositories[type_repos]["file"] = "dummy-ensemble.yaml"
    if imports_tpl and isinstance(imports_tpl, list):
        for imp_def in imports_tpl:
            if isinstance(imp_def, dict) and "repository" in imp_def:
                repository = imp_def["repository"]
                if (
                    repository in repositories_tpl
                    and repository != "unfurl"
                    and repository not in repositories
                ):
                    repositories[repository] = repositories_tpl[repository]
                    repositories[repository]["file"] = imp_def["file"].partition("#")[0]
    if repositories:
        db["repositories"] = repositories


def _to_graphql(
    localenv: LocalEnv,
    root_url: Optional[str] = None,
    include_all: bool = False,
) -> Tuple[GraphqlDB, YamlManifest, DeploymentEnvironment, ResourceTypesByName]:
    # set skip_validation because we want to be able to dump incomplete service templates
    manifest = localenv.get_manifest(skip_validation=True, safe_mode=True)
    db = GraphqlDB({})
    spec = manifest.tosca
    assert spec
    tpl = spec.template.tpl
    assert spec.topology and tpl
    _add_repositories(db, tpl)
    assert spec.template.topology_template
    if root_url:
        url: Optional[str] = root_url
    else:
        url = manifest.get_package_url()
    types = ResourceTypesByName(url or "", spec.template.topology_template.custom_defs)
    to_graphql_nodetypes(spec, include_all, types)
    db["ResourceType"] = types  # type: ignore
    db["ResourceTemplate"] = {}
    environment_instances = {}
    # the ensemble will have merged in its environment's templates and types
    connection_types = ResourceTypesByName(
        url or "", spec.template.topology_template.custom_defs
    )
    for node_spec in spec.topology.node_templates.values():
        toscaEntityTemplate = node_spec.toscaEntityTemplate
        # XXX default templates can be from different namespace
        t = nodetemplate_to_json(node_spec, types)
        if t.get("visibility") == "omit":
            continue
        name = t["name"]
        if "virtual" in toscaEntityTemplate.directives:
            # virtual is only set when loading resources from an environment
            # or from "spec/resource_templates"
            environment_instances[name] = t
            typename = types.expand_typename(node_spec.global_type)
            if typename not in types:
                logger.error(
                    "Connection type %s for resource %s is missing",
                    typename,
                    node_spec.nested_name,
                )
            else:
                connection_types[typename] = types[typename]
        else:
            db["ResourceTemplate"][name] = t
    connections: Dict[ResourceTemplateName, ResourceTemplate] = {}
    assert spec.topology
    for relationship_spec in spec.topology.relationship_templates.values():
        template = cast(RelationshipTemplate, relationship_spec.toscaEntityTemplate)
        if template.default_for:
            type_definition = template.type_definition
            assert type_definition
            typename = types.get_typename(type_definition)
            if typename in types:
                connection_types[typename] = types[typename]
            elif typename not in connection_types:
                node_type_to_graphql(spec.topology, type_definition, connection_types)
            # this is actually a RelationshipTemplate
            connection_template = nodetemplate_to_json(
                relationship_spec, connection_types
            )
            if connection_template.get("visibility") == "omit":
                continue
            name = connection_template["name"]
            assert name not in db["ResourceTemplate"], f"template name conflict: {name}"
            # db["ResourceTemplate"][name] = connection_template
            connections[name] = connection_template

    db["Overview"] = tpl.get("metadata") or {}
    env = DeploymentEnvironment(
        connections=connections,
        primary_provider=connections.get("primary_provider"),
        instances=environment_instances,
        repositories=manifest.context.get("repositories") or {},
    )
    assert manifest.repo
    file_path = manifest.get_tosca_file_path()
    if root_url and include_all:
        # if include_all, assume this export is for another repository
        # (see get_types in server.py)
        for ty in types.values():
            add_root_source_info(ty, root_url, file_path)
        for ty in connection_types.values():
            if ty:
                add_root_source_info(ty, root_url, file_path)
    return db, manifest, env, connection_types


def to_blueprint(
    localEnv: LocalEnv,
    root_url: Optional[str] = None,
    include_all=False,
    *,
    file: Optional[str] = None,
    nested: bool = False,
) -> GraphqlDB:
    db, manifest, env, env_types = _to_graphql(localEnv, root_url, include_all)
    blueprint, root_name = to_graphql_blueprint(
        assert_not_none(manifest.tosca), db, nested
    )
    deployment_blueprints = get_deployment_blueprints(
        manifest, blueprint, root_name, db
    )
    db["DeploymentTemplate"] = deployment_blueprints
    blueprint["deploymentTemplates"] = list(deployment_blueprints)
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    return db


# NB! to_deployment is the default export format used by __main__.export (but you won't find that via grep)
def to_deployment(
    localEnv: LocalEnv, root_url: Optional[str] = None, *, file: Optional[str] = None
) -> GraphqlDB:
    start_time = perf_counter()
    logger.debug("Exporting deployment %s", localEnv.manifestPath)
    db, manifest, env, env_types = _to_graphql(localEnv, root_url)
    blueprint, dtemplate = get_blueprint_from_topology(manifest, db)
    db["DeploymentTemplate"] = {dtemplate["name"]: dtemplate}
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    db.add_graphql_deployment(manifest, dtemplate, nodetemplate_to_json)
    db["ResourceType"].update(env_types)  # type: ignore
    # don't include base node type:
    db["ResourceType"].pop("tosca.nodes.Root")
    env_instances = manifest.context.get("instances")
    if env_instances:
        env["instances"].update(_set_shared_instances(env_instances, env_types))
    db["DeploymentEnvironment"] = env  # type: ignore
    logger.verbose(
        f"finished exporting deployment %s in {perf_counter() - start_time:.3f}s)",
        localEnv.manifestPath,
    )
    return db


def mark_leaf_types(types: ResourceTypesByName) -> None:
    # types without implementations can't be instantiated by users
    # treat non-leaf types as abstract types that shouldn't be instatiated
    # treat leaf types that have requirements as having implementations
    counts: Counter = Counter()
    for rt in types.values():
        if rt.get("metadata") and rt["metadata"].get("alias"):  # type: ignore
            continue  # exclude alias types from the count
        for name in rt["extends"]:
            counts[name] += 1
    for name, count in counts.most_common():
        jsontype = types.get(name)
        if not jsontype:
            continue
        if count == 1:
            # not subtyped so assume its a concrete type
            if not jsontype.get("implementations") and jsontype.get("requirements"):
                # hack: if we're a "compound" type that is just a sum of its requirement, add an implementation so it is included
                jsontype["implementations"] = ["create"]
        elif jsontype.get("implementations") and not jsontype.get("metadata", {}).get(
            "concrete"
        ):
            # abstract types can't be instantiated
            jsontype["implementations"] = []


def _node_typename_to_graphql(
    reqtypename: str,
    topology: TopologySpec,
    types: ResourceTypesByName,
) -> Optional[ResourceType]:
    # XXX!
    custom_defs = topology.topology_template.custom_defs
    typedef = types._make_typedef(reqtypename)
    if typedef:
        return node_type_to_graphql(topology, typedef, types)
    return None


def annotate_requirements(
    topology: TopologySpec,
    types: ResourceTypesByName,
    descendents: TypeDescendants,
) -> None:
    for jsontype in list(types.values()):  # _annotate_requirement() might modify types
        for req in jsontype.get("requirements") or []:
            _annotate_requirement(
                req, req["resourceType"], topology, types, descendents
            )


def _annotate_requirement(
    req: RequirementConstraint,
    reqtypename: TypeName,
    topology: TopologySpec,
    types: ResourceTypesByName,
    descendents: Optional[TypeDescendants],
) -> None:
    # req is a RequirementConstraint dict
    if "node_filter" not in req:
        return
    node_filter = req["node_filter"]
    req_filters = node_filter.get("requirements")
    match_filters = node_filter.get("match")
    if match_filters:
        match = match_filters[0]  # XXX support more than one
        if isinstance(match, str):
            # override the resource template
            req["match"] = match
        elif list(match)[0] == "get_nodes_of_type":
            # override the resourceType
            type_match = match["get_nodes_of_type"]
            reqtype = types.get(reqtypename)
            if not reqtype:
                reqtype = _node_typename_to_graphql(type_match, topology, types)
            if reqtype:
                req["resourceType"] = TypeName(reqtype["name"])
            else:
                logger.error("couldn't find type for get_nodes_of_type %s", type_match)
                req["resourceType"] = types.expand_typename(type_match)

    reqtype = types.get(reqtypename)
    if not reqtype:
        reqtype = _node_typename_to_graphql(reqtypename, topology, types)
        if not reqtype:
            logger.error("couldn't find %s while annotating requirements", reqtypename)
            return

    # node_filter properties might refer to properties that are only present on some subtypes
    prop_filters: Dict = {}
    for typename in reqtype["extends"]:
        if typename not in types:
            continue
        subtype: ResourceType = types[typename]
        inputsSchemaProperties = subtype["inputsSchema"]["properties"]
        _map_nodefilter_properties(req, inputsSchemaProperties, prop_filters)

    if prop_filters:
        req["inputsSchema"] = dict(properties=prop_filters)
    if req_filters:
        req["requirementsFilter"] = [
            _make_req(rf, topology, types, reqtypename, descendents)[2]
            for rf in req_filters
        ]
    req.pop("node_filter")


def _map_nodefilter_properties(filters, inputsSchemaProperties, jsonprops) -> dict:
    ONE_TO_ONE_MAP = dict(object="map", array="list")
    # {i["name"]: inputsSchemaProperties
    for name, value in get_nodefilters(filters, "properties"):
        if not isinstance(value, dict) or "eval" in value:
            # the filter declared an expression to set the property's value
            # mark the property to be deleted from the target inputsSchema so the user can't set it
            # (since they can't shouldn't set this property now)
            jsonprops[name] = None
        elif "default" in value:
            jsonprops.setdefault(name, {})["default"] = value["default"]
        elif name not in jsonprops and name in inputsSchemaProperties:
            # check name in jsonprops so we only do this once
            # use the inputs schema definition to determine the node filter's constraints
            schema = inputsSchemaProperties[name]
            tosca_datatype = schema.get("$toscatype") or ONE_TO_ONE_MAP.get(
                schema["type"], schema["type"]
            )
            constraints = ConditionClause(name, value, tosca_datatype).conditions
            jsonprops.setdefault(name, {}).update(
                map_constraints(schema["type"], constraints, schema)
            )
    return jsonprops


def _set_shared_instances(instances, types: ResourceTypesByName):
    env_instances = {}
    # imported (aka shared) resources are just a reference and won't be part of the manifest unless referenced
    # so just copy them over now
    if instances:
        for instance_name, value in instances.items():
            if "imported" in value:
                if types:
                    value["type"] = types.expand_typename(value["type"])
                env_instances[instance_name] = value
    return env_instances


def to_environments(
    localEnv: LocalEnv,
    root_url: Optional[str] = "",
    environment_filter=None,
    *,
    file: Optional[str] = None,
) -> GraphqlDB:
    """
    Map the environments in the project's unfurl.yaml to a json collection of Graphql objects.

    Unlike the other JSON exports, the ResourceTemplates are included inline
    instead of referenced by name (so the json can be included directly into unfurl.yaml).

    Each registered ensembles will be represents as a DeploymentPath object
    with the ensemble's path as its name.

    Also include ResourceType objects for any types referenced in the environments' connections and instances.
    """

    # XXX one manifest and blueprint per environment
    db = GraphqlDB.load_db(file)
    environments = {}
    all_connection_types: GraphqlObjectsByName = {}
    assert localEnv.project
    localEnv.overrides["load_env_instances"] = True
    deployment_paths = get_deploymentpaths(localEnv.project)
    env_deployments = {}
    for ensemble_info in deployment_paths.values():
        if not environment_filter or environment_filter == ensemble_info["environment"]:
            env_deployments[ensemble_info["environment"]] = ensemble_info["name"]
    assert localEnv.project
    defaults = localEnv.project.contexts.get("defaults")
    default_imported_instances = None
    default_manifest_path = localEnv.manifestPath
    for name in localEnv.project.contexts:
        if environment_filter and environment_filter != name:
            continue
        try:
            # reuse the localEnv and use the default manifest so environment instances don't clash with a real deployment
            localEnv.manifest_context_name = name
            # delete existing default manfest if created because we need to instantiate a different ToscaSpec object
            localEnv._manifests.pop(default_manifest_path, None)
            localEnv.manifestPath = default_manifest_path
            blueprintdb, manifest, env, env_types = _to_graphql(localEnv, root_url)
            env["name"] = name
            if default_imported_instances is None:
                default_imported_instances = _set_shared_instances(
                    defaults and defaults.get("instances"), env_types
                )
            env["instances"].update(default_imported_instances)
            instances = localEnv.project.contexts[name].get("instances")
            env["instances"].update(_set_shared_instances(instances, env_types))
            environments[name] = env
            all_connection_types.update(env_types)  # type: ignore # ok to merge qualified names
        except Exception as err:
            logger.error("error exporting environment %s", name, exc_info=True)
            details = "".join(traceback.TracebackException.from_exception(err).format())
            environments[name] = dict(error="Internal Error", details=details)  # type: ignore

    db["DeploymentEnvironment"] = environments  # type: ignore
    db["ResourceType"] = all_connection_types
    db["DeploymentPath"] = deployment_paths  # type: ignore
    return db


def to_deployments(
    localEnv: LocalEnv, ignored=None, *, file: Optional[str] = None
) -> DeploymentPaths:
    assert localEnv.project

    db = GraphqlDB.get_deployment_paths(localEnv.project)
    deployments: List[GraphqlDB] = []
    db["deployments"] = deployments
    for manifest_path, dp in db["DeploymentPath"].items():
        # start_time = perf_counter()
        try:
            # optimization: reuse localenv
            localEnv.manifestPath = os.path.join(
                localEnv.project.projectRoot, manifest_path, "ensemble.yaml"
            )
            environment = dp.get("environment") or "defaults"
            localEnv.manifest_context_name = environment
            deployments.append(to_deployment(localEnv))
        except Exception:
            logger.error("error exporting deployment %s", manifest_path, exc_info=True)
        # _print(f"{perf_counter() - start_time}s export for {manifest_path}")

    # from .yamlloader import yaml_perf
    # _print(f"exported {len(deployments)} deployments, {yaml_perf:.3f} seconds parsing yaml")
    # from .manifest import _cache
    # _print("cached files with access count", json.dumps([(k, v[1]) for k, v in _cache.items()], indent=2))
    return db


def get_deploymentpaths(project: Project) -> Dict[str, DeploymentPath]:
    return GraphqlDB.get_deployment_paths(project)["DeploymentPath"]
