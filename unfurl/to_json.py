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
    Union,
    cast,
    TYPE_CHECKING,
)
from collections import Counter
from collections.abc import Mapping, MutableSequence
from typing_extensions import TypedDict
from urllib.parse import urlparse
from toscaparser.imports import is_url
from toscaparser.substitution_mappings import SubstitutionMappings
from toscaparser.properties import Property
from toscaparser.elements.constraints import Schema
from toscaparser.elements.property_definition import PropertyDef
from toscaparser.elements.nodetype import NodeType
from toscaparser.elements.relationshiptype import RelationshipType
from toscaparser.elements.statefulentitytype import StatefulEntityType
from toscaparser.entity_template import EntityTemplate
from toscaparser.common.exception import ExceptionCollector
from toscaparser.elements.scalarunit import get_scalarunit_class
from toscaparser.elements.datatype import DataType
from toscaparser.elements.portspectype import PortSpec
from toscaparser.activities import ConditionClause
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.relationship_template import RelationshipTemplate
from .repo import sanitize_url
from .runtime import EntityInstance, TopologyInstance
from .yamlmanifest import YamlManifest
from .merge import merge_dicts, patch_dict
from .logs import sensitive, is_sensitive, getLogger
from .tosca import (
    EntitySpec,
    NodeSpec,
    TopologySpec,
    ToscaSpec,
    is_function,
    get_nodefilters,
)
from .util import to_enum, UnfurlError
from .support import Status, is_template
from .result import ChangeRecord
from .localenv import LocalEnv, Project

logger = getLogger("unfurl")


GraphqlObject = NewType("GraphqlObject", Dict[str, Any])
GraphqlObjectsByName = Dict[str, GraphqlObject]
ResourceType = NewType("ResourceType", GraphqlObject)
ResourceTypesByName = Dict[str, ResourceType]
GraphqlDB = NewType("GraphqlDB", Dict[str, GraphqlObjectsByName])
CustomDefs = Dict[str, dict]


def _get_types(db: GraphqlDB) -> ResourceTypesByName:
    return cast(ResourceTypesByName, db["ResourceType"])


def _print(*args):
    print(*args, file=sys.stderr)


def js_timestamp(ts: datetime.datetime) -> str:
    jsts = ts.isoformat(" ", "seconds")
    if ts.utcoffset() is None:
        # if no timezone assume utc and append the missing offset
        return jsts + "+00:00"
    return jsts


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
        value = scalar_class(
            str(value) + scalar_class.SCALAR_UNIT_DEFAULT
        ).get_num_from_scalar_unit(unit)
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
        # "scalar-unit.bitrate": _SCALAR_TYPE, # XXX add parser support 3.3.6.7 scalar-unit.bitrate
        "tosca.datatypes.network.PortDef": VALUE_TYPES["PortDef"],
        "tosca.datatypes.network.PortSpec": VALUE_TYPES["PortSpec"],
    }
)


def tosca_type_to_jsonschema(custom_defs: CustomDefs, propdefs, toscatype):
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


def tosca_schema_to_jsonschema(p: PropertyDef, custom_defs: CustomDefs):
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
    if toscaSchema.default is not None and not is_server_only_expression(toscaSchema.default):
        schema["default"] = toscaSchema.default
    if toscaSchema.required:
        schema["required"] = True
    if toscaSchema.description:
        schema["description"] = toscaSchema.description

    metadata = toscaSchema.schema.get("metadata")
    if metadata:
        # set "sensitive" if present
        schema.update(metadata)
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
            propdefs = [
                p
                for p in dt.get_properties_def_objects()
                if is_property_user_visible(p)
            ]
            type_schema = tosca_type_to_jsonschema(custom_defs, propdefs, dt.type)
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
        nodespec = topology.get_node_template(node)
        if nodespec:
            # if there's already a resource template assigned and it is marked internal
            node_metadata = (
                nodespec.toscaEntityTemplate.entity_tpl.get("metadata") or {}
            )
            if not node_metadata.get("user_settable") and not metadata.get(
                "user_settable"
            ):
                return "hidden"
            else:
                return "visible"
    return "inherit"


def _get_req(req_dict) -> Tuple[str, dict]:
    name, req = list(req_dict.items())[0]
    if isinstance(req, str):
        req = dict(node=req)
    else:
        assert isinstance(req, dict), f"bad {req} in {req_dict}"
    return name, req


def expand_prefix(nodetype: str):
    return nodetype.replace("tosca:", "tosca.nodes.")  # XXX


def _find_req_typename(types: ResourceTypesByName, typename, reqname) -> str:
    # have types[typename] go first
    for target_reqs in types[typename]["requirements"]:
        if target_reqs["name"] == reqname:
            return target_reqs["resourceType"]

    # XXX not very efficient
    for t in types.values():
        # mark_leaf_types() sets implementations, assume its has already been called
        if "requirements" in t and not t["implementations"]:
            continue  # not a leaf type
        if typename in t["extends"]:
            # type is typename or derived from typename
            for target_reqs in t["requirements"]:
                if target_reqs["name"] == reqname:
                    return target_reqs["resourceType"]
    return ""  # not found


def _make_req(
    req_dict: dict,
    topology: Optional[TopologySpec] = None,
    types: Optional[ResourceTypesByName] = None,
    typename: Optional[str] = None,
) -> Tuple[str, dict, GraphqlObject]:
    name, req = _get_req(req_dict)
    reqobj = GraphqlObject(
        dict(
            name=name,
            description=req.get("description") or "",
            __typename="RequirementConstraint",
        )
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

    if req.get("node_filter"):
        reqobj["node_filter"] = req["node_filter"]
        if types is not None:
            assert topology
            assert typename is not None
            # we're called from annotate_requirements annotating a nested requirements constraint
            reqtypename = _find_req_typename(types, typename, name)
            if reqtypename:
                _annotate_requirement(reqobj, reqtypename, topology, types)

    return name, req, reqobj


def requirement_to_graphql(
    topology: TopologySpec, req_dict: dict, include_omitted = False
) -> Optional[GraphqlObject]:
    """
    type RequirementConstraint {
        name: String!
        title: String
        description: String
        resourceType: ResourceType!
        match: ResourceTemplate
        min: Int
        max: Int
        badge: String
        visibility: String
        icon: String
        inputsSchema: JSON
        requirementsFilter: [RequirementConstraint!]
    }
    """
    name, req, reqobj = _make_req(req_dict)
    if "min" not in reqobj:
        # set defaults
        reqobj["min"] = 1
        reqobj["max"] = 1

    visibility = _requirement_visibility(topology, name, req)
    if not include_omitted and visibility == "omit":
        return None

    if visibility != "inherit":
        reqobj["visibility"] = visibility

    if reqobj["max"] == 0:
        return None

    reqobj["match"] = None
    nodetype = req.get("node")
    if nodetype:
        # req['node'] can be a node_template instead of a type
        if nodetype in topology.node_templates:
            reqobj["match"] = nodetype
            nodetype = topology.node_templates[nodetype].type
        nodetype = expand_prefix(nodetype)
    else:
        nodetype = req.get("capability")
        if not nodetype:
            # XXX check for "relationship" and get it's valid_target_types
            logger.warning(
                "skipping constraint %s, there was no type specified ", req_dict
            )
            return None
    reqobj["resourceType"] = nodetype
    return reqobj


def _get_extends(
    topology: TopologySpec,
    typedef: StatefulEntityType,
    extends: list,
    types: Optional[ResourceTypesByName],
) -> None:
    if not typedef:
        return
    name = typedef.type
    if name not in extends:
        extends.append(name)
    if types is not None and name not in types:
        node_type_to_graphql(topology, typedef, types)
    ExceptionCollector.collecting = True
    for p in typedef.parent_types():
        _get_extends(topology, p, extends, types)


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


def is_property_user_visible(p: PropertyDef) -> bool:
    user_settable = p.schema.get("metadata", {}).get("user_settable")
    if user_settable is not None:
        return user_settable
    if p.default is not None or is_computed(p):
        return False
    return True


def is_server_only_expression(value) -> bool:
    if isinstance(value, list):
        return any(is_function(item) or is_template(item) for item in value)
    if _is_front_end_expression(value):  # special case for client
        return False
    return is_function(value) or is_template(value)


def is_computed(p) -> bool:  # p: Property | PropertyDef
    # XXX be smarter about is_computed() if the user should be able to override the default
    if isinstance(p.schema, Schema):
        metadata = p.schema.metadata
    else:
        metadata = p.schema.get("metadata") or {}
    return (
        p.name in ["tosca_id", "state", "tosca_name"]
        or is_server_only_expression(p.value)
        or (p.value is None and is_server_only_expression(p.default))
        or metadata.get("computed")
    )


def always_export(p: Property) -> bool:
    if isinstance(p.schema, Schema):
        metadata = p.schema.metadata
    else:
        metadata = p.schema.get("metadata") or {}
    return bool(metadata.get("export"))


def maybe_export_value(prop: Property, instance: EntityInstance, attrs: List[dict]):
    export = always_export(prop)
    if export:
        # evaluate computed property now
        try:
            value = instance.attributes[prop.name]
        except Exception as e:
            # this can be raised if the evaluation is unsafe
            logger.warning(
                f"export could not evaluate property {prop.name} on {instance}: {e}"
            )
        else:
            attrs.append(
                dict(
                    name=prop.name,
                    value=attribute_value_to_json(prop, value),
                )
            )
    return export


# def property_value_to_json(p, value):
#     if is_computed(p):
#         return None
#     return attribute_value_to_json(p, value)


def _is_front_end_expression(value) -> bool:
    if isinstance(value, dict):
        if "eval" in value:
            expr = value['eval']
            return "abspath" in expr or "get_dir" in expr
        else:
            return "get_env" in value or "secret" in value or "_generate" in value
    return False


class PropertyVisitor:
    redacted = False
    user_settable = False

    def redact_if_sensitive(self, value):
        if isinstance(value, Mapping):
            return {key: self.redact_if_sensitive(v) for key, v in value.items()}
        elif isinstance(value, (MutableSequence, tuple)):
            return [self.redact_if_sensitive(item) for item in value]
        elif is_sensitive(value):
            self.redacted = True
            return sensitive.redacted_str
        else:
            return value

    def attribute_value_to_json(self, p, value):
        if p.schema.metadata.get("sensitive"):
            if is_server_only_expression(value) or _is_front_end_expression(value):
                return value
            self.redacted = True
            return sensitive.redacted_str

        if isinstance(value, PortSpec):
            return value.spec
        elif isinstance(value, dict) and p.type in [
            "tosca.datatypes.network.PortSpec",
            "PortSpec",
        ]:
            return PortSpec.make(value).spec
        scalar_class = get_scalarunit_class(p.type)
        if scalar_class:
            unit = p.schema.metadata.get("default_unit")
            if unit:  # convert to the default_unit
                return scalar_class(value).get_num_from_scalar_unit(unit)
        return self.redact_if_sensitive(value)


def attribute_value_to_json(p, value):
    return PropertyVisitor().attribute_value_to_json(p, value)


def _get_source_info(source_info: dict) -> dict:
    _import = {}
    root = source_info["root"]
    prefix = source_info.get("prefix")
    if prefix:
        _import["prefix"] = prefix
    repository = source_info.get("repository")
    if repository:
        _import["repository"] = repository
        _import["url"] = root
        _import["file"] = source_info["file"]
    else:
        path = source_info["path"]
        base = source_info["base"]
        # make path relative to the import base (not the file that imported)
        # and include the fragment if present
        # base and path will both be local file paths
        _import["file"] = path[len(base) :].strip('/') + "".join(
            source_info["file"].partition("#")[1:]
        )
        if is_url(root):
            _import["url"] = root
        # otherwise import relative to main service template
    return _import


def add_root_source_info(jsontype: ResourceType, repo_url: str, base_path: str) -> None:
    # if the type wasn't imported add source info pointing at the root service template
    source_info = jsontype.get("_sourceinfo")
    if not source_info:
        # not an import, type defined in main service template file
        # or it's an import relative to the root, just include the root import because it will in turn import this import
        jsontype["_sourceinfo"] = dict(url=sanitize_url(repo_url, False), file=base_path)
    elif "url" not in source_info:
        # it's an import relative to the root, set to the repo's url
        source_info["url"] = sanitize_url(repo_url, False)

# XXX outputs: only include "public" attributes?
def node_type_to_graphql(
    topology: TopologySpec,
    type_definition: StatefulEntityType,
    types: ResourceTypesByName,
    summary: bool = False,
) -> ResourceType:
    """
    type ResourceType {
      name: String!
      title: String
      extends: [ResourceType!]
      description: String
      badge: String
      icon: String
      visibility: String
      details_url: String
      inputsSchema: JSON
      computedPropertiesSchema: JSON
      outputsSchema: JSON
      requirements: [RequirementConstraint!]
      implementations: [String]
      implementation_requirements: [String]
      _sourceinfo: JSON
    }
    """
    custom_defs = topology.topology_template.custom_defs
    typename = type_definition.type
    jsontype = ResourceType(
        GraphqlObject(
            dict(
                name=typename,  # fully qualified name
                title=type_definition.type.split(".")[-1],  # short, readable name
                description=type_definition.get_value("description") or "",
            )
        )
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
        if "title" in metadata:
            jsontype["title"] = metadata["title"]
        if metadata.get("internal"):
            visibility = "hidden"

    if typename in custom_defs:
        _source = custom_defs[typename].get("_source")
        if isinstance(_source, dict):  # could be str or None
            jsontype["_sourceinfo"] = _get_source_info(_source)

    if type_definition.defs is None:
        logger.warning("%s is missing type definition", type_definition.type)
        return jsontype

    extends: List[str] = []
    # add ancestors classes to extends
    _get_extends(topology, type_definition, extends, types)
    if isinstance(type_definition, NodeType):
        # add capabilities types to extends
        for cap in type_definition.get_capability_typedefs():
            _get_extends(topology, cap, extends, None)
    jsontype["extends"] = extends

    operations = set(
        op.name
        for op in EntityTemplate._create_interfaces(type_definition, None)
        if op.interfacetype != "Mock"
    )
    jsontype["implementations"] = sorted(operations)
    jsontype[
        "implementation_requirements"
    ] = type_definition.get_interface_requirements()

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

    if not type_definition.is_derived_from("tosca.nodes.Root"):
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
                requirement_to_graphql(topology, req)
                for req in type_definition.get_all_requirements()
            ],
        )
    )
    return jsontype


def _make_typedef(
    typename: str, custom_defs: CustomDefs
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
    return typedef


def _update_root_type(jsontype: GraphqlObject, sub_map: SubstitutionMappings):
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
    names = sub_map.get_declared_requirement_names()
    for req in jsontype.get("requirements", []):
        if req["name"] in names:
            req["min"] = 0
    # templates created with this type need to have the substitute directive
    jsontype["directives"] = ["substitute"]


def to_graphql_nodetypes(spec: ToscaSpec, include_all: bool) -> ResourceTypesByName:
    # node types are readonly, so mapping doesn't need to be bijective
    types = cast(ResourceTypesByName, {})
    topology = spec.topology
    assert topology
    custom_defs = topology.topology_template.custom_defs
    ExceptionCollector.collecting = True
    # create these ones first
    # XXX detect error later if these types are being used elsewhere
    for nested_topology in spec.nested_topologies:
        sub_map = nested_topology.topology_template.substitution_mappings
        if sub_map:
            typedef = sub_map.node_type
            if typedef:
                jsontype = node_type_to_graphql(nested_topology, typedef, types)
                _update_root_type(jsontype, sub_map)

    if include_all:
        for typename in custom_defs:
            if typename in types:
                continue
            typedef = _make_typedef(typename, custom_defs)
            if typedef:
                node_type_to_graphql(topology, typedef, types)
        for typename in StatefulEntityType.TOSCA_DEF:  # builtin types
            if typename.startswith("unfurl.nodes") or typename.startswith(
                "unfurl.relationships"
            ):
                if typename in types:
                    continue
                # only include our extensions
                typedef = _make_typedef(typename, custom_defs)
                if typedef:
                    node_type_to_graphql(topology, typedef, types)

    for node_spec in topology.node_templates.values():
        type_definition = node_spec.toscaEntityTemplate.type_definition
        typename = type_definition.type
        if typename not in types:
            node_type_to_graphql(topology, type_definition, types)

    mark_leaf_types(types)
    assert spec.topology
    annotate_requirements(spec.topology, types)

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


def add_capabilities_as_properties(schema, nodetype, custom_defs: CustomDefs):
    """treat each capability as a property and add them to props"""
    for cap in nodetype.get_capability_typedefs():
        if not cap.get_definition("properties"):
            continue
        propdefs = [
            p for p in cap.get_properties_def_objects() if is_property_user_visible(p)
        ]
        schema[cap.name] = tosca_type_to_jsonschema(custom_defs, propdefs, cap.type)


def add_capabilities_as_attributes(schema, nodetype, custom_defs: CustomDefs):
    """treat each capability as an attribute and add them to props"""
    for cap in nodetype.get_capability_typedefs():
        if not cap.get_definition("attributes"):
            continue
        propdefs = [
            p for p in cap.get_attributes_def_objects() if is_property_user_visible(p)
        ]
        schema[cap.name] = tosca_type_to_jsonschema(custom_defs, propdefs, cap.type)


def _get_typedef(name: str, custom_defs: CustomDefs) -> Optional[StatefulEntityType]:
    typedef = custom_defs.get(name)
    if not typedef:
        typedef = StatefulEntityType.TOSCA_DEF.get(name)
    return typedef


def template_properties_to_json(nodetemplate: NodeTemplate, visitor):
    # if they aren't only include ones with an explicity value
    for p in nodetemplate.get_properties_objects():
        computed = is_computed(p)
        if computed and not _is_front_end_expression(p.value):
            # don't expose values that are expressions to the user
            value = None
        else:
            value = visitor.attribute_value_to_json(p, p.value)
        user_visible = is_property_user_visible(p)
        if user_visible:
            visitor.user_settable = True
        else:
            if p.value is None or p.value == p.default:
                # assumed to not be set, just use the default value and skip
                continue
            if computed:
                # preserve the expression
                value = p.value
        yield dict(name=p.name, value=value)


def _get_requirement(
    req: dict, nodespec: EntitySpec, types: ResourceTypesByName
) -> Optional[GraphqlObject]:
    name, req_dict = _get_req(req)
    # we need to call _get_explicit_relationship() to make sure all the requirements relationships are created
    # _get_explicit_relationship returns req_dict merged with its type definition
    req_dict, rel_template = nodespec.toscaEntityTemplate._get_explicit_relationship(
        name, req_dict
    )
    if nodespec.type not in types:
        typeobj = _node_typename_to_graphql(nodespec.type, nodespec.topology, types)
        if not typeobj:
            return None
    else:
        typeobj = types[nodespec.type]

    match = req_dict.get("node")
    reqconstraint = reqconstaint_from_nodetemplate(nodespec, name, req_dict)
    if reqconstraint is None:
        return None

    if "substitute" in typeobj.get("directives", []):
        # we've might have modified the type in _update_root_type()
        # and the tosca object won't know about that change so set it now
        for typeconstraint in typeobj["requirements"]:
            if name == typeconstraint["name"]:
                reqconstraint["min"] = typeconstraint["min"]

    _annotate_requirement(
        reqconstraint, reqconstraint["resourceType"], nodespec.topology, types
    )
    reqjson = GraphqlObject(
        dict(constraint=reqconstraint, name=name, match=None, __typename="Requirement")
    )
    if match and not _get_typedef(
        match, nodespec.topology.topology_template.custom_defs
    ):
        # it's not a type, assume it's a node template
        # (the node template name might not match a template if it is only defined in the deployment blueprints)
        reqjson["match"] = match
    return reqjson


def reqconstaint_from_nodetemplate(nodespec: EntitySpec, name: str, req_dict: dict):
    if "constraint" in req_dict.get("metadata", {}):
        # this happens when we import graphql json directly
        reqconstraint = req_dict["metadata"]["constraint"]
    else:
        # "node" on a node template's requirement in TOSCA yaml is the node match so don't use as part of the constraint
        # use the type's "node" (unless the requirement isn't defined on the type at all)
        typeReqDef = nodespec.toscaEntityTemplate.type_definition.get_requirement_definition(name)
        if typeReqDef:
            # preserve node as "match" for _requirement_visibility
            req_dict["match"] = req_dict.get("node")
            if "node" in typeReqDef:
                req_dict["node"] = typeReqDef["node"]
            elif "capability" in req_dict:
                req_dict.pop("node", None)
            # else: empty typeReqDef, leave req_dict alone
        reqconstraint = requirement_to_graphql(nodespec.topology, {name: req_dict})
    return reqconstraint


def _find_typename(
    nodetemplate: EntityTemplate,
    types: ResourceTypesByName,
) -> str:
    # XXX
    # if we substituting template, generate a new type
    # json type for root of imported blueprint only include unfulfilled requirements
    # and set defaults on properties set by the inner root
    return nodetemplate.type


def nodetemplate_to_json(
    node_spec: EntitySpec,
    types: ResourceTypesByName,
    for_resource: bool = False,
) -> GraphqlObject:
    """
    Returns json object as a ResourceTemplate:

    type ResourceTemplate {
      name: String!
      title: String
      type: ResourceType!
      visibility: String
      directives: [String!]
      imported: String

      description: string

      # Maps to an object that conforms to type.inputsSchema
      properties: [Input!]

      dependencies: [Requirement!]
    }

    type Requirement {
      name: String!
      constraint: RequirementConstraint!
      match: ResourceTemplate
      target: Resource
    }
    """
    nodetemplate = node_spec.toscaEntityTemplate
    spec = node_spec.spec
    if "__typename" in nodetemplate.entity_tpl:
        # previously imported from the json, just return it
        ogprops = nodetemplate.entity_tpl.pop("_original_properties", None)
        if ogprops is not None:
            nodetemplate.entity_tpl["properties"] = ogprops
        return nodetemplate.entity_tpl

    json = GraphqlObject(
        dict(
            type=_find_typename(nodetemplate, types),
            name=nodetemplate.name,
            title=nodetemplate.entity_tpl.get("metadata", {}).get("title")
            or nodetemplate.name,
            description=nodetemplate.entity_tpl.get("description") or "",
            directives=nodetemplate.directives,
            # __typename="ResourceTemplate",  # XXX
        )
    )
    if "imported" in nodetemplate.entity_tpl:
        json["imported"] = nodetemplate.entity_tpl.get("imported")

    visitor = PropertyVisitor()
    json["properties"] = list(template_properties_to_json(nodetemplate, visitor))
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

    if not nodetemplate.type_definition.is_derived_from("tosca.nodes.Root"):
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
    for req in nodetemplate.all_requirements:
        reqjson = _get_requirement(req, node_spec, types)
        if reqjson is None:
            continue
        if reqjson["constraint"].get("visibility") != "hidden":
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


def _generate_primary(spec: ToscaSpec, db: GraphqlDB, node_tpl=None) -> NodeTemplate:
    base_type = node_tpl["type"] if node_tpl else "tosca.nodes.Root"
    topology = spec.template.topology_template
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
    roots = [
        node.toscaEntityTemplate
        for node in spec.topology.node_templates.values()
        if not node.toscaEntityTemplate.get_relationship_templates()
        and "default" not in node.directives
    ]
    nodetype_tpl["requirements"] = [{node.name: dict(node=node.type)} for node in roots]

    topology.custom_defs[primary_name] = nodetype_tpl
    tpl = node_tpl or {}
    tpl["type"] = primary_name
    tpl.setdefault("properties", {}).update(
        {name: dict(get_input=name) for name in topology._tpl_inputs()}
    )
    # if create new template, need to assign the nodes explicitly (needed if multiple templates have the same type)
    if not node_tpl:
        tpl["requirements"] = [{node.name: dict(node=node.name)} for node in roots]
    node_spec = spec.topology.add_node_template(primary_name, tpl, False)
    node_template = cast(NodeTemplate, node_spec.toscaEntityTemplate)

    types = _get_types(db)
    assert node_template.type_definition
    node_type_to_graphql(spec.topology, node_template.type_definition, types)
    db["ResourceTemplate"][node_template.name] = nodetemplate_to_json(node_spec, types)
    return node_template


# if a node type or template is specified, use that, but it needs to be compatible with the generated type
def _get_or_make_primary(spec: ToscaSpec, db: GraphqlDB) -> Tuple[str, str]:
    ExceptionCollector.start()  # topology.add_template may generate validation exceptions
    assert spec.template
    topology = spec.template.topology_template
    assert topology
    # we need to generate a root template
    root_type = None
    root = None
    if topology.substitution_mappings:
        root_type = topology.substitution_mappings.node_type
        root = topology.substitution_mappings._node_template

    if root_type:
        properties_tpl = root_type.get_definition("properties") or {}
        # if no property mapping in use, generate a new root template if there are any missing inputs
        if (
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
                    node_spec, _get_types(db)
                )
    else:
        root = _generate_primary(spec, db)

    assert root
    return root.name, root.type


def blueprint_metadata(spec: ToscaSpec, root_name: str) -> GraphqlObject:
    title, name = _template_title(spec, root_name)
    blueprint = GraphqlObject(dict(name=name, title=title))
    blueprint["description"] = spec.template.description
    metadata = spec.template.tpl.get("metadata") or {}
    blueprint["livePreview"] = metadata.get("livePreview")
    blueprint["sourceCodeUrl"] = metadata.get("sourceCodeUrl")
    blueprint["image"] = metadata.get("image")
    blueprint["projectIcon"] = metadata.get("projectIcon")
    blueprint["primaryDeploymentBlueprint"] = metadata.get("primaryDeploymentBlueprint")
    return blueprint


def to_graphql_blueprint(spec: ToscaSpec, db: GraphqlDB) -> Tuple[GraphqlObject, str]:
    """
    Returns json object as ApplicationBlueprint

    ApplicationBlueprint: {
      name: String!
      title: String
      description: String
      primary: ResourceType!
      primaryDeploymentBlueprint: String
      deploymentTemplates: [DeploymentTemplate!]

      livePreview: String
      sourceCodeUrl: String
      image: String
      projectIcon: String
    }
    """
    # note: root_resource_template is derived from inputs, outputs and substitution_template from topology_template
    root_name, root_type = _get_or_make_primary(spec, db)
    blueprint = blueprint_metadata(spec, root_name)
    blueprint["__typename"] = "ApplicationBlueprint"
    blueprint["primary"] = root_type
    blueprint["deploymentTemplates"] = []  # type: ignore
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


def _generate_env_names(spec: ToscaSpec, root_name: str) -> Iterator[str]:
    assert spec.topology
    primary = spec.topology.get_node_template(root_name)
    assert primary
    custom_defs = spec.template.topology_template.custom_defs
    primary_type = primary.toscaEntityTemplate.type_definition
    yield from _generate_env_names_from_type("APP", primary_type, custom_defs)
    # primary.toscaEntityTemplate.type_definition
    for req in primary_type.get_all_requirements():
        # get the target type
        req_json = requirement_to_graphql(spec.topology, req)
        if not req_json:
            continue
        name = req_json["name"]
        target_type = req_json["resourceType"]
        typedef = _make_typedef(target_type, custom_defs)
        assert typedef, target_type
        yield from _generate_env_names_from_type(name.upper(), typedef, custom_defs)


def get_deployment_blueprints(
    manifest: YamlManifest, blueprint: GraphqlObject, root_name: str, db: GraphqlDB
) -> dict:
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

    type DeploymentTemplate {
      name: String!
      title: String!
      slug: String!
      description: String

      blueprint: ApplicationBlueprint!
      primary: ResourceTemplate!
      resourceTemplates: [ResourceTemplate!]
      cloud: ResourceType
      environmentVariableNames: [String!]
    }
    """
    deployment_blueprints = (
        manifest.manifest.expanded.get("spec", {}).get("deployment_blueprints") or {}
    )
    spec = manifest.tosca
    assert spec and spec.topology
    deployment_templates = {}
    for name, tpl in deployment_blueprints.items():
        slug = slugify(name)
        template = tpl.copy()
        local_resource_templates = {}
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
                t = nodetemplate_to_json(node_spec, _get_types(db))
                if t.get("visibility") == "omit":
                    continue
                local_resource_templates[node_name] = t

        # XXX assert that db["ResourceTemplate"] has already has all the node_templates
        # names of ResourceTemplates:
        resourceTemplates = sorted(
            set(db["ResourceTemplate"]) | set(local_resource_templates)
        )
        primary = tpl.get("primary") or root_name
        env_vars = list(_generate_env_names(spec, primary))
        template.update(
            dict(
                __typename="DeploymentTemplate",
                title=tpl.get("title") or name,
                description=tpl.get("description"),
                name=name,
                slug=slug,
                blueprint=blueprint["name"],
                primary=primary,
                resourceTemplates=resourceTemplates,
                ResourceTemplate=local_resource_templates,
                environmentVariableNames=env_vars,
            )
        )
        deployment_templates[name] = template
    return deployment_templates


def _project_path(url):
    pp = urlparse(url).path.lstrip("/")
    if pp.endswith(".git"):
        return pp[:-4]
    return pp


def get_blueprint_from_topology(manifest: YamlManifest, db: GraphqlDB):
    spec = manifest.tosca
    assert spec
    # XXX cloud = spec.topology.primary_provider
    blueprint, root_name = to_graphql_blueprint(spec, db)
    templates = get_deployment_blueprints(manifest, blueprint, root_name, db)

    template = {}
    deployment_blueprint_name = manifest.context.get("deployment_blueprint")
    if deployment_blueprint_name and deployment_blueprint_name in templates:
        deployment_blueprint = templates[deployment_blueprint_name]
        template = deployment_blueprint.copy()

    # the deployment template created for this deployment will have a "source" key
    # so if it doesn't (or one wasn't set) create a new one and set the current one as its "source"
    if "source" not in template:
        title = (
            os.path.basename(os.path.dirname(manifest.path))
            if manifest.path
            else "untitled"
        )
        slug = slugify(title)
        template.update(
            dict(
                __typename="DeploymentTemplate",
                title=title,
                name=slug,
                slug=slug,
                description=spec.template.description,
                # names of ResourceTemplates
                resourceTemplates=sorted(db["ResourceTemplate"]),
                ResourceTemplate={},
                source=deployment_blueprint_name,
                projectPath=_project_path(manifest.repositories["spec"].url),
            )
        )
        templates[slug] = template

    blueprint["deploymentTemplates"] = list(templates)
    template["blueprint"] = blueprint["name"]
    template["primary"] = root_name
    template["environmentVariableNames"] = list(_generate_env_names(spec, root_name))
    last_commit_time = manifest.last_commit_time()
    if last_commit_time:
        template["commitTime"] = js_timestamp(last_commit_time)
    return blueprint, template


def _to_graphql(
    localEnv: LocalEnv,
    root_url: str = "",
) -> Tuple[GraphqlDB, YamlManifest, GraphqlObject, ResourceTypesByName]:
    # set skip_validation because we want to be able to dump incomplete service templates
    manifest = localEnv.get_manifest(skip_validation=True, safe_mode=True)
    db = GraphqlDB({})
    spec = manifest.tosca
    assert spec
    tpl = spec.template.tpl
    assert spec.topology and tpl
    types_repo = tpl.get("repositories").get("types")
    if types_repo:  # only export types, avoid built-in repositories
        db["repositories"] = {"types": types_repo}
    types = to_graphql_nodetypes(spec, bool(root_url))
    db["ResourceType"] = types
    db["ResourceTemplate"] = {}
    environment_instances = {}
    connection_types = cast(ResourceTypesByName, {})
    for node_spec in spec.topology.node_templates.values():
        toscaEntityTemplate = node_spec.toscaEntityTemplate
        t = nodetemplate_to_json(node_spec, types)
        if t.get("visibility") == "omit":
            continue
        name = t["name"]
        if "virtual" in toscaEntityTemplate.directives:
            # virtual is only set when loading resources from an environment
            # or from "spec/resource_templates"
            environment_instances[name] = t
            typename = toscaEntityTemplate.type_definition.type
            connection_types[typename] = types[typename]
        else:
            db["ResourceTemplate"][name] = t

    connections = {}
    assert spec.topology
    for relationship_spec in spec.topology.relationship_templates.values():
        template = cast(RelationshipTemplate, relationship_spec.toscaEntityTemplate)
        if template.default_for:
            type_definition = template.type_definition
            assert type_definition
            typename = type_definition.type
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
    env = GraphqlObject(
        dict(
            connections=connections,
            primary_provider=connections.get("primary_provider"),
            instances=environment_instances,
            repositories=manifest.context.get("repositories") or {},
        )
    )
    assert manifest.repo
    file_path = manifest.get_tosca_file_path()
    if root_url:
        # if include_all, assume this export is for another repository
        # (see get_types in server.py)
        for t in types.values():
            add_root_source_info(t, root_url, file_path)
        for t in connection_types.values():
            add_root_source_info(t, root_url, file_path)
    return db, manifest, env, connection_types


def _add_resources(
    db: GraphqlDB,
    relationships: Dict[str, List[GraphqlObject]],
    root: TopologyInstance,
    resources: List[GraphqlObject],
    shadowed: Optional[EntityInstance],
) -> None:
    for instance in root.get_self_and_descendants():
        # don't include the nested template that is a shadow of the outer template
        if instance is not root and instance is not shadowed:
            resource = to_graphql_resource(instance, db, relationships)
            if not resource:
                continue
            resources.append(resource)
            if cast(NodeSpec, instance.template).substitution and instance.shadow:
                assert instance.shadow.root is not instance.root
                _add_resources(
                    db,
                    relationships,
                    cast(TopologyInstance, instance.shadow.root),
                    resources,
                    instance.shadow,
                )


def _add_lastjob(last_job: dict, deployment: GraphqlObject) -> None:
    readyState = last_job.get("readyState")
    workflow = last_job.get("workflow")
    deployment["workflow"] = workflow
    if isinstance(readyState, dict):
        deployment["status"] = to_enum(
            Status, readyState.get("effective", readyState.get("local"))
        )
        if workflow == "undeploy" and deployment["status"] == Status.ok:
            deployment["status"] = Status.absent
    deployment["summary"] = last_job.get("summary")
    deployTimeString = last_job.get("endTime", last_job["startTime"])
    deployment["deployTime"] = js_timestamp(
        datetime.datetime.strptime(deployTimeString, ChangeRecord.DateTimeFormat)
    )


def _set_deployment_url(
    manifest, deployment: GraphqlObject, primary_resource: Optional[GraphqlObject]
):
    outputs = manifest.get_saved_outputs()
    if outputs and "url" in outputs:
        url = outputs["url"]
    else:
        # computed outputs might not be saved, so try to evaluate it now
        try:
            url = manifest.rootResource.attributes["outputs"].get("url")
        except UnfurlError as e:
            url = None  # this can be raised if the evaluation is unsafe
            logger.warning(f"export could not evaluate output 'url': {e}")

    if url:
        deployment["url"] = url
    elif primary_resource and primary_resource.get("attributes"):
        for prop in primary_resource["attributes"]:
            if prop["name"] == "url":
                deployment["url"] = prop["value"]
                break


def add_graphql_deployment(
    manifest: YamlManifest, db: GraphqlDB, dtemplate
) -> GraphqlObject:
    """
    type Deployment {
      title: String!
      primary: Resource
      resources: [Resource!]
      deploymentTemplate: DeploymentTemplate!
      url: url
      status: Status
      summary: String
      workflow: String
      deployTime: String
    }
    """
    name = dtemplate["name"]
    title = dtemplate.get("title") or name
    deployment = GraphqlObject(dict(name=name, title=title, __typename="Deployment"))
    templates = db["ResourceTemplate"]
    relationships: Dict[str, List[GraphqlObject]] = {}
    for t in templates.values():
        if "dependencies" in t:
            for c in t["dependencies"]:
                if c.get("match"):
                    relationships.setdefault(c["match"], []).append(c)
    assert manifest.rootResource
    resources: List[GraphqlObject] = []
    _add_resources(db, relationships, manifest.rootResource, resources, None)
    db["Resource"] = {r["name"]: r for r in resources}
    deployment["resources"] = list(db["Resource"])
    primary_name = deployment["primary"] = dtemplate["primary"]
    deployment["deploymentTemplate"] = dtemplate["name"]
    if manifest.lastJob:
        _add_lastjob(manifest.lastJob, deployment)

    primary_resource = db["Resource"].get(primary_name)
    _set_deployment_url(manifest, deployment, primary_resource)
    if primary_resource and primary_resource["title"] == primary_name:
        primary_resource["title"] = deployment["title"]
    db["Deployment"] = {name: deployment}
    return deployment


def to_blueprint(localEnv: LocalEnv, root_url: Optional[str] = None) -> GraphqlDB:
    db, manifest, env, env_types = _to_graphql(localEnv, root_url or "")
    assert manifest.tosca
    blueprint, root_name = to_graphql_blueprint(manifest.tosca, db)
    deployment_blueprints = get_deployment_blueprints(
        manifest, blueprint, root_name, db
    )
    db["DeploymentTemplate"] = deployment_blueprints
    blueprint["deploymentTemplates"] = list(deployment_blueprints)
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    return db


# NB! to_deployment is the default export format used by __main__.export (but you won't find that via grep)
def to_deployment(localEnv: LocalEnv, existing: Optional[str] = None) -> GraphqlDB:
    logger.debug("exporting deployment %s", localEnv.manifestPath)
    db, manifest, env, env_types = _to_graphql(localEnv)
    blueprint, dtemplate = get_blueprint_from_topology(manifest, db)
    db["DeploymentTemplate"] = {dtemplate["name"]: dtemplate}
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    add_graphql_deployment(manifest, db, dtemplate)
    # don't include base node type:
    db["ResourceType"].pop("tosca.nodes.Root")
    logger.debug("finished exporting deployment %s", localEnv.manifestPath)
    return db


def mark_leaf_types(types) -> None:
    # types without implementations can't be instantiated by users
    # treat non-leaf types as abstract types that shouldn't be instatiated
    # treat leaf types that have requirements as having implementations
    counts: Counter = Counter()
    for rt in types.values():
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
        elif jsontype.get("implementations"):
            jsontype["implementations"] = []


def _node_typename_to_graphql(
    reqtypename: str,
    topology: TopologySpec,
    types: ResourceTypesByName,
) -> Optional[ResourceType]:
    custom_defs = topology.topology_template.custom_defs
    typedef = _make_typedef(reqtypename, custom_defs)
    if typedef:
        return node_type_to_graphql(topology, typedef, types)
    return None


def annotate_requirements(topology: TopologySpec, types: ResourceTypesByName) -> None:
    for jsontype in list(types.values()):  # _annotate_requirement() might modify types
        for req in jsontype.get("requirements") or []:
            _annotate_requirement(req, req["resourceType"], topology, types)


def _annotate_requirement(
    req: GraphqlObject,
    reqtypename: str,
    topology: TopologySpec,
    types: ResourceTypesByName,
) -> None:
    # req is a RequirementConstraint dict
    reqtype = types.get(reqtypename)
    if not reqtype:
        reqtype = _node_typename_to_graphql(reqtypename, topology, types)
        if not reqtype:
            return

    if "node_filter" not in req:
        return
    node_filter = req["node_filter"]
    req_filters = node_filter.get("requirements")
    # node_filter properties might refer to properties that are only present on some subtypes
    prop_filters: Dict = {}
    for typename in reqtype["extends"]:
        if typename not in types:
            continue
        subtype: Dict = types[typename]
        inputsSchemaProperties = subtype["inputsSchema"]["properties"]
        _map_nodefilter_properties(req, inputsSchemaProperties, prop_filters)

    if prop_filters:
        req["inputsSchema"] = dict(properties=prop_filters)
    if req_filters:
        req["requirementsFilter"] = [
            _make_req(rf, topology, types, reqtypename)[2] for rf in req_filters
        ]
    req.pop("node_filter")


def _map_nodefilter_properties(filters, inputsSchemaProperties, jsonprops) -> dict:
    ONE_TO_ONE_MAP = dict(object="map", array="list")
    # {i["name"]: inputsSchemaProperties
    for name, value in get_nodefilters(filters, "properties"):
        if not isinstance(value, dict) or "eval" in value:
            # the filter declared an expression to set the property's value
            # mark the property to be deleted from the target inputschema so the user can't set it
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


def _set_shared_instances(instances):
    env_instances = {}
    # imported (aka shared) resources are just a reference and won't be part of the manifest unless referenced
    # so just copy them over now
    if instances:
        for instance_name, value in instances.items():
            if "imported" in value:
                env_instances[instance_name] = value
    return env_instances


def to_environments(localEnv: LocalEnv, existing: Optional[str] = None) -> GraphqlDB:
    """
    Map the environments in the project's unfurl.yaml to a json collection of Graphql objects.
    Each environment is be represented as:

    type DeploymentEnvironment {
      name: String!
      connections: [ResourceTemplate!]
      instances: [ResourceTemplate!]
      primary_provider: ResourceTemplate
      repositories: JSON!
    }

    Unlike the other JSON exports, the ResourceTemplates are included inline
    instead of referenced by name (so the json can be included directly into unfurl.yaml).

    Each registered ensembles will be represents as a DeploymentPath object
    with the ensemble's path as its name.

    Also include ResourceType objects for any types referenced in the environments' connections and instances.
    """

    # XXX one manifest and blueprint per environment
    db = _load_db(existing)
    environments = {}
    all_connection_types = cast(ResourceTypesByName, {})
    blueprintdb = None
    assert localEnv.project
    deployment_paths = get_deploymentpaths(localEnv.project)
    env_deployments = {}
    for ensemble_info in deployment_paths.values():
        env_deployments[ensemble_info["environment"]] = ensemble_info["name"]
    assert localEnv.project
    defaults = localEnv.project.contexts.get("defaults")
    default_imported_instances = _set_shared_instances(
        defaults and defaults.get("instances")
    )
    default_manifest_path = localEnv.manifestPath
    for name in localEnv.project.contexts:
        try:
            # we can reuse the localEnv if there's a distinct manifest that uses this environment
            localEnv.manifest_context_name = name
            if name in env_deployments:
                localEnv.manifestPath = os.path.join(
                    localEnv.project.projectRoot, env_deployments[name], "ensemble.yaml"
                )
            else:
                # this environment doesn't have any deployments, reuse the default one
                # delete existing because we need to instantiate a different ToscaSpec object
                localEnv._manifests.pop(default_manifest_path, None)
                localEnv.manifestPath = default_manifest_path
            localLocalEnv = localEnv
            blueprintdb, manifest, env, env_types = _to_graphql(localLocalEnv)
            env["name"] = name
            env["instances"].update(default_imported_instances)
            instances = localEnv.project.contexts[name].get("instances")
            env["instances"].update(_set_shared_instances(instances))
            environments[name] = env
            all_connection_types.update(env_types)
        except Exception as err:
            logger.error("error exporting environment %s", name, exc_info=True)
            details = "".join(
                traceback.TracebackException.from_exception(err).format()
            )
            environments[name] = dict(error="Internal Error", details=details)  # type: ignore

    db["DeploymentEnvironment"] = environments
    if blueprintdb:
        # XXX re-enable this?
        # if blueprintdb.get("repositories", {}).get("types"):
        #     # don't include ResourceTypes if we are including a types repository
        #     db["ResourceType"] = all_connection_types
        #     return db

        # add the rest of the types too
        # XXX is it safe to only include types with "connect" implementations?
        all_connection_types.update(_get_types(blueprintdb))
    db["ResourceType"] = cast(GraphqlObjectsByName, all_connection_types)
    db["DeploymentPath"] = deployment_paths
    return db


class DeploymentPaths(TypedDict):
    DeploymentPath: GraphqlObjectsByName


class Deployments(DeploymentPaths):
    deployments: List[GraphqlDB]


def to_deployments(localEnv: LocalEnv, existing: Optional[str] = None) -> Deployments:
    assert localEnv.project
    db = cast(Deployments, set_deploymentpaths(localEnv.project))
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
    # _print(f"exported {len(deployments)} deployments, {yaml_perf} seconds parsing yaml")
    # from .manifest import _cache
    # _print("cached files with access count", json.dumps([(k, v[1]) for k, v in _cache.items()], indent=2))
    return db


def get_deploymentpaths(project: Project) -> GraphqlObjectsByName:
    return set_deploymentpaths(project)["DeploymentPath"]


def _load_db(existing: Optional[str]) -> GraphqlDB:
    if existing and os.path.exists(existing):
        with open(existing) as f:
            return cast(GraphqlDB, json.load(f))
    else:
        return GraphqlDB({})


def set_deploymentpaths(
    project: Project, existing: Optional[str] = None
) -> DeploymentPaths:
    """
    Deployments identified by their file path.

    type DeploymentPath {
      name: String!
      environment: String!
      project_id: String
      pipelines: [JSON!]
      incremental_deploy: boolean!
    }
    """
    db = cast(DeploymentPaths, _load_db(existing))
    deployment_paths = db.setdefault("DeploymentPath", {})
    for ensemble_info in project.localConfig.ensembles:
        if "environment" in ensemble_info and "project" not in ensemble_info:
            # exclude external ensembles
            path = os.path.dirname(ensemble_info["file"])
            if os.path.isabs(path):
                path = project.get_relative_path(path)
            obj = GraphqlObject(
                {
                    "__typename": "DeploymentPath",
                    "name": path,
                    "project_id": ensemble_info.get("project_id"),
                    "pipelines": ensemble_info.get("pipelines", []),
                    "environment": ensemble_info["environment"],
                    "incremental_deploy": ensemble_info.get(
                        "incremental_deploy", False
                    ),
                }
            )
            if path in deployment_paths:
                # merge duplicate entries
                deployment_paths[path].update(obj)
            else:
                deployment_paths[path] = obj
    return db


def add_attributes(instance: EntityInstance) -> List[Dict[str, Any]]:
    attrs = []
    attributeDefs = instance.template.attributeDefs.copy()
    # instance._attributes only has values that were set by the instance, not spec properties or attribute defaults
    # instance._attributes should already be serialized
    if instance.shadow:
        _attributes = instance.shadow._attributes
    else:
        _attributes = instance._attributes
    if _attributes:
        for name, value in _attributes.items():
            p = attributeDefs.pop(name, None)
            if not p:
                # check if this attribute is shadowing a property
                p = instance.template.propertyDefs.get(name)
                if not p:
                    # same as EntityTemplate._create_properties()
                    p = Property(
                        name,
                        value,
                        dict(type="any"),
                        instance.template.toscaEntityTemplate.custom_def,
                    )
            if not p.schema.get("metadata", {}).get("internal"):
                attrs.append(dict(name=p.name, value=attribute_value_to_json(p, value)))
    # add leftover attribute defs that have a default value
    for prop in attributeDefs.values():
        if prop.default is not None:
            if not maybe_export_value(prop, instance, attrs) and not is_computed(prop):
                attrs.append(
                    dict(
                        name=prop.name,
                        value=attribute_value_to_json(prop, prop.default),
                    )
                )
    return attrs


def add_computed_properties(instance: EntityInstance) -> List[Dict[str, Any]]:
    attrs = []
    if instance.shadow:
        _attributes = instance.shadow._attributes
    else:
        _attributes = instance._attributes
    # instance._properties should already be serialized
    _properties = instance._properties or {}
    if _properties:
        for name, value in _properties.items():
            if name in _attributes:  # shadowed, don't include both
                continue
            p = instance.template.propertyDefs.get(name)
            if not p:
                # same as EntityTemplate._create_properties()
                p = Property(
                    name,
                    value,
                    dict(type="any"),
                    instance.template.toscaEntityTemplate.custom_def,
                )
            if p.schema.get("metadata", {}).get("visibility") != "hidden":
                if is_sensitive(value) and _is_front_end_expression(p.value):
                    value = p.value
                else:
                    value = attribute_value_to_json(p, value)
                attrs.append(dict(name=p.name, value=value))

    for prop in instance.template.propertyDefs.values():
        if prop.default is not None and prop.name not in instance._properties:
            if prop.name not in instance.template.attributeDefs:
                maybe_export_value(prop, instance, attrs)

    return attrs


def is_resource_real(instance):
    for resource_types in ["unfurl.nodes.CloudObject", "unfurl.nodes.K8sRawResource"]:
        if instance.template.is_compatible_type(resource_types):
            return True
    if (
        "id" in instance.attributes
        or "console_url" in instance.attributes
        or "url" in instance.attributes
    ):
        return True
    return False


def to_graphql_resource(
    instance: EntityInstance,
    db: GraphqlDB,
    relationships: Dict[str, List[GraphqlObject]],
) -> Optional[GraphqlObject]:
    """
    type Resource {
      name: String!
      title: String!
      url: String
      template: ResourceTemplate!
      status: Status
      state: State
      attributes: [Input!]
      computedProperties: [Input!]
      connections: [Requirement!]
      protected: Boolean
      imported: String
    }
    """
    # XXX url
    template = db["ResourceTemplate"].get(instance.template.name)
    pending = not instance.status or instance.status == Status.pending
    if not template:
        if pending or "virtual" in instance.template.directives:
            return None
        template = nodetemplate_to_json(
            instance.template,
            _get_types(db),
            True,
        )
        if template.get("visibility") == "omit":
            return None
        db["ResourceTemplate"][instance.template.name] = template

    resource = GraphqlObject(
        dict(
            name=instance.nested_name,
            title=template.get("title", instance.name),
            template=instance.template.name,
            state=instance.state,
            status=instance.status,
            __typename="Resource",
            imported=instance.imported,
        )
    )

    if "visibility" in template and template["visibility"] != "inherit":
        resource["visibility"] = template["visibility"]
    else:
        if instance.template.name in relationships:
            visibilities = set(
                reqjson["constraint"].get("visibility", "inherit")
                for reqjson in relationships[instance.template.name]
            )
            # visible visibilities override hidden
            if len(visibilities) == 1 and "hidden" in visibilities:
                # if the relationship was hidden only include the resource if its "real"
                if not is_resource_real(instance):
                    resource["visibility"] = "hidden"
        else:
            if pending:
                if instance.template.aggregate_only():
                    resource["status"] = Status.ok
                else:
                    # this resource wasn't referenced, don't include if it wasn't part of the plan
                    # XXX: have a more accurate way to figure this out
                    return None
    logger.debug(
        f"exporting resource {instance.name} with visibility {resource.get('visibility')}"
    )

    resource["protected"] = instance.protected
    resource["attributes"] = add_attributes(instance)
    resource["computedProperties"] = add_computed_properties(instance)

    if template.get("dependencies"):
        requirements = {r["name"]: r.copy() for r in template["dependencies"]}
        # XXX there is a bug where this can't find instances sometimes:
        # so for now just copy match if it exists
        # for req in instance.template.requirements.values():
        #     # test because it might not be set if the template is incomplete
        #     if req.relationship and req.relationship.target:
        #         requirements[req.name]["target"] = req.relationship.target.name
        for req in requirements.values():
            if req.get("match"):
                req["target"] = req["match"]
        resource["connections"] = list(requirements.values())
    else:
        resource["connections"] = []
    return resource
