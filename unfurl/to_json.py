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
import sys
from typing import Any, Dict, List, Optional, Union, cast, TYPE_CHECKING
from collections import Counter
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
from .logs import sensitive
from .tosca import is_function, get_nodefilters
from .localenv import LocalEnv
from .util import to_enum
from .support import Status, is_template


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
        value = scalar_class(str(value) + scalar_class.SCALAR_UNIT_DEFAULT).get_num_from_scalar_unit(unit)
    if key == "schema":
        return value
    elif key == "pattern":
        return dict(pattern=value)
    elif key == "equal":
        return dict(const=value)
    elif key == "valid_values":
        return dict(enum=value)
    elif key in numeric_constraints:
        return {numeric_constraints[key]: value}
    elif key in ["in_range", "length", "min_length", "max_length"]:
        suffix = dict(string="Length", object="Properties", array="Items", number="imum")[jsonType]
        prefix = key[:3]
        if key == "in_range":
            start, stop = value
            if unit:
                # values have already been converted to the base unit, now we need to convert it back to the default_unit
                scalar_class = get_scalarunit_class(schema["$toscatype"])
                start = scalar_class(str(start) + scalar_class.SCALAR_UNIT_DEFAULT).get_num_from_scalar_unit(unit)
                stop = scalar_class(str(stop) + scalar_class.SCALAR_UNIT_DEFAULT).get_num_from_scalar_unit(unit)
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
VALUE_TYPES = dict(
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
    PortSpec={"type": "string"}
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


def tosca_type_to_jsonschema(spec, propdefs, toscatype):
    jsonschema = dict(
        type="object",
        properties={p.name: tosca_schema_to_jsonschema(p, spec) for p in propdefs},
    )
    # add typeid:
    if toscatype:
        jsonschema["properties"].update({"$toscatype": dict(const=toscatype)})
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


def get_scalar_unit(value_type, metadata):
    default_unit = metadata and metadata.get("default_unit")
    if default_unit:
        return {"type": "number", "default_unit": default_unit}
    else:
        return {"type": "string"}
    # XXX add format specifier
    # regex: "|".join(get_scalarunit_class(value_type).SCALAR_UNIT_DICT)


def tosca_schema_to_jsonschema(p, spec):
    # convert a PropertyDef to a property (this creates the Schema object we need)
    prop = Property(
        p.name,
        p.default,
        p.schema or dict(type="string"),
        spec.template.topology_template.custom_defs,
    )
    toscaSchema = prop.schema
    tosca_type = toscaSchema.type
    schema = {}
    if toscaSchema.title or p.name:
        schema["title"] = toscaSchema.title or p.name
    if toscaSchema.default is not None and not is_value_computed(toscaSchema.default):
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
        dt = DataType(tosca_type, spec.template.topology_template.custom_defs)
        if dt.value_type:  # it's a simple type
            type_schema, valuetype_schema = get_valuetype(dt, metadata)
            if valuetype_schema.constraints:  # merge constraints
                constraints = valuetype_schema.constraints + constraints
        else:  # it's a complex type with properties:
            type_schema = tosca_type_to_jsonschema(
                spec, dt.get_properties_def_objects(), dt.type
            )
    if tosca_type not in ONE_TO_ONE_TYPES and "properties" not in schema:
        schema["$toscatype"] = tosca_type
    schema.update(type_schema)

    if toscaSchema.entry_schema:
        entrySchema = tosca_schema_to_jsonschema(
            PropertyDef("", None, toscaSchema.entry_schema), spec
        )
        if tosca_type == "list":
            schema["items"] = entrySchema
        else:
            schema["additionalProperties"] = entrySchema

    if constraints:
        schema.update(map_constraints(schema["type"], toscaSchema.constraints, schema))
    return schema


def _requirement_visibility(spec, name, req):
    if name == "dependency":
        return "omit"
    node = req.get("node")
    metadata = req.get("metadata") or {}
    if metadata.get("visibility"):
        return metadata["visibility"]
    if metadata.get('internal'):
        return "hidden"
    if node and node in spec.nodeTemplates:
        # if there's already a resource template assigned and it is marked internal
        node_metadata = spec.nodeTemplates[node].toscaEntityTemplate.entity_tpl.get('metadata') or {}
        if not node_metadata.get('user_settable') and not metadata.get('user_settable'):
            return "hidden"
        else:
            return "visible"
    return "inherit"

def _get_req(req_dict):
    name, req = list(req_dict.items())[0]
    if isinstance(req, str):
        req = dict(node=req)
    else:
        assert isinstance(req, dict), f"bad {req} in {req_dict}"
    return name, req

def requirement_to_graphql(spec, req_dict):
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
    }
    """
    name, req = _get_req(req_dict)
    visibility = _requirement_visibility(spec, name, req)
    if visibility == "omit":
        return None

    reqobj = dict(name=name, title=name, description=req.get("description") or "")
    if visibility != "inherit":
        reqobj["visibility"] = visibility
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
    else:
        reqobj["min"] = 1
        reqobj["max"] = 1

    reqobj["match"] = None
    nodetype = req.get("node")
    if nodetype:
        # req['node'] can be a node_template instead of a type
        if nodetype in spec.nodeTemplates:
            reqobj["match"] = nodetype
            nodetype = spec.nodeTemplates[nodetype].type
    else:
        nodetype = req.get("capability") or "tosca.nodes.Root"
    reqobj["resourceType"] = nodetype
    if req.get("node_filter"):
        reqobj["node_filter"] = req["node_filter"]
    return reqobj


def _get_extends(spec, typedef, extends: list, types):
    if not typedef:
        return
    name = typedef.type
    if name not in extends:
        extends.append(name)
    if types is not None and name not in types:
        node_type_to_graphql(spec, typedef, types)
    for p in typedef.parent_types():
        _get_extends(spec, p, extends, types)


def resource_visibility(spec, t):
    if "default" in t.directives:
        # XXX: directives aren't preserved in export so
        # if default isn't omitted, it will lose its default directive if the export is included
        return "hidden"
    metadata = t.entity_tpl.get('metadata')
    if metadata and metadata.get('internal'):
        return "hidden"
    if spec.discovered and t.name in spec.discovered:
        return "omit"
    return "inherit"


def is_property_user_visible(p):
    user_settable = p.schema.get("metadata", {}).get("user_settable")
    if user_settable is not None:
        return user_settable
    if p.default is not None or is_computed(p):
        return False
    return True


def is_value_computed(value):
    if isinstance(value, list):
        return any(is_function(item) or is_template(item) for item in value)
    return is_function(value) or is_template(value)


def is_computed(p): # p: Property | PropertyDef
    # XXX be smarter about is_computed() if the user should be able to override the default
    if isinstance(p.schema, Schema):
        metadata = p.schema.metadata
    else:
        metadata = p.schema.get("metadata") or {}
    return (
        p.name in ["tosca_id", "state", "tosca_name"]
        or is_value_computed(p.value)
        or is_value_computed(p.default)
        or metadata.get("computed")
    )


# def property_value_to_json(p, value):
#     if is_computed(p):
#         return None
#     return attribute_value_to_json(p, value)

def _is_get_env_or_secret(value):
    return isinstance(value, dict) and ('get_env' in value or 'secret' in value)

def attribute_value_to_json(p, value):
    if isinstance(value, sensitive) or p.schema.metadata.get('sensitive'):
        if _is_get_env_or_secret(value):
            return value
        return sensitive.redacted_str
    if isinstance(value, PortSpec):
        return value.spec
    elif isinstance(value, dict) and p.type in ["tosca.datatypes.network.PortSpec", "PortSpec"]:
        return PortSpec.make(value).spec
    scalar_class = get_scalarunit_class(p.type)
    if scalar_class:
        unit = p.schema.metadata.get("default_unit")
        if unit:  # convert to the default_unit
            return scalar_class(value).get_num_from_scalar_unit(unit)
    return value


# XXX outputs: only include "public" attributes?
def node_type_to_graphql(spec, type_definition, types: dict):
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
    }
    """
    jsontype = dict(
        name=type_definition.type,  # fully qualified name
        title=type_definition.type.split(".")[-1],  # short, readable name
        description=type_definition.get_value("description") or "",
    )
    types[type_definition.type] = jsontype # set now to avoid circular reference via _get_extends
    metadata = type_definition.get_value("metadata")
    visibility = "inherit"
    if metadata:
        if "badge" in metadata:
            jsontype["badge"] = metadata["badge"]
        if "icon" in metadata:
            jsontype["icon"] = metadata["icon"]
        if "title" in metadata:
            jsontype["title"] = metadata["title"]
        if "details_url" in metadata:
            jsontype["details_url"] = metadata["details_url"]
        if metadata.get("internal"):
            visibility = "hidden"
    jsontype["visibility"] = visibility

    propertydefs = list(
        (p, is_property_user_visible(p)) for p in type_definition.get_properties_def_objects()
    )
    jsontype["inputsSchema"] = tosca_type_to_jsonschema(spec, (p[0] for p in propertydefs if p[1]), None)
    jsontype["computedPropertiesSchema"] = tosca_type_to_jsonschema(spec, (p[0] for p in propertydefs if not p[1]), None)

    extends = []
    # add ancestors classes to extends
    _get_extends(spec, type_definition, extends, types)
    jsontype["extends"] = extends
    if not type_definition.is_derived_from("tosca.nodes.Root"):
        return jsontype

    # add capabilities types to extends
    for cap in type_definition.get_capability_typedefs():
        _get_extends(spec, cap, extends, None)

    # treat each capability as a complex property
    add_capabilities_as_properties(
        jsontype["inputsSchema"]["properties"], type_definition, spec
    )
    # XXX only include "public" attributes?
    attributedefs = (
        p for p in type_definition.get_attributes_def_objects() if is_property_user_visible(p)
    )
    jsontype["outputsSchema"] = tosca_type_to_jsonschema(spec, attributedefs, None)
    # XXX capabilities can hava attributes too
    # add_capabilities_as_attributes(jsontype["outputs"], type_definition, spec)

    jsontype["requirements"] = list(filter(None, [
        requirement_to_graphql(spec, req)
        for req in type_definition.get_all_requirements()
    ]))

    operations = set(
        op.name for op in EntityTemplate._create_interfaces(type_definition, None)
        if op.interfacetype != "Mock"
    )
    jsontype["implementations"] = sorted(operations)
    jsontype[
        "implementation_requirements"
    ] = type_definition.get_interface_requirements()
    return jsontype


def _make_typedef(typename, custom_defs):
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


def to_graphql_nodetypes(spec):
    # node types are readonly, so mapping doesn't need to be bijective
    types = {}
    custom_defs = spec.template.topology_template.custom_defs
    for typename in custom_defs:
        if typename in types:
            continue
        typedef = _make_typedef(typename, custom_defs)
        if typedef:
            node_type_to_graphql(spec, typedef, types)
    for typename in StatefulEntityType.TOSCA_DEF: # builtin types
        if typename.startswith('unfurl.nodes') or typename.startswith('unfurl.relationships'):
            if typename in types:
                continue
            # only include our extensions
            typedef = _make_typedef(typename, custom_defs)
            if typedef:
                node_type_to_graphql(spec, typedef, types)

    for node_spec in spec.nodeTemplates.values():
        type_definition = node_spec.toscaEntityTemplate.type_definition
        typename = type_definition.type
        if typename not in types:
            node_type_to_graphql(spec, type_definition, types)

    mark_leaf_types(types)
    annotate_properties(types)
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


def add_capabilities_as_properties(schema, nodetype, spec):
    """treat each capability as a property and add them to props"""
    for cap in nodetype.get_capability_typedefs():
        if not cap.get_definition("properties"):
            continue
        schema[cap.name] = tosca_type_to_jsonschema(
            spec, cap.get_properties_def_objects(), cap.type
        )


def add_capabilities_as_attributes(schema, nodetype, spec):
    """treat each capability as an attribute and add them to props"""
    for cap in nodetype.get_capability_typedefs():
        if not cap.get_definition("attributes"):
            continue
        schema[cap.name] = tosca_type_to_jsonschema(
            spec, cap.get_attributes_def_objects(), cap.type
        )


def _find_requirement_constraint(reqs, name):
    for reqconstraint in reqs:
        if reqconstraint["name"] == name:
            return reqconstraint

    # no requirement defined, use default
    return dict(
        name=name,
        min=1,
        max=1,
        resourceType="tosca.nodes.Root",
        __typename="RequirementConstraint",
    )


def _get_typedef(name: str, spec):
    typedef = spec.template.topology_template.custom_defs.get(name)
    if not typedef:
        typedef = StatefulEntityType.TOSCA_DEF.get(name)
    return typedef


def template_properties_to_json(nodetemplate):
    # if they aren't only include ones with an explicity value
    for p in nodetemplate.get_properties_objects():
        computed = is_computed(p)
        if computed and not _is_get_env_or_secret(p.value):
            # don't expose values that are expressions to the user
            value = None
        else:
            value = attribute_value_to_json(p, p.value)
        if not is_property_user_visible(p):
            if p.value is None or p.value == p.default:
                # assumed to not be set, just use the default value and skip
                continue
            if computed:
                # preserve the expression
                value = p.value
        yield dict(name=p.name, value=value)


def nodetemplate_to_json(nodetemplate, spec, types):
    """
    Returns json object as a ResourceTemplate:

    type ResourceTemplate {
      name: String!
      title: String
      type: ResourceType!
      visibility: String
      directives: [String!]

      description: string

      # Maps to an object that conforms to type.inputsSchema
      properties: [Input!]

      dependencies: [Requirement!]
    }

    type Requirement {
      name: String!
      constraint: RequirementConstraint!
      visibility: String
      match: ResourceTemplate
      target: Resource
    }
    """
    if '__typename' in nodetemplate.entity_tpl:
        # previously imported from the json, just return it
        ogprops = nodetemplate.entity_tpl.pop("_original_properties", None)
        if ogprops is not None:
            nodetemplate.entity_tpl['properties'] = ogprops
        return nodetemplate.entity_tpl

    json = dict(
        type=nodetemplate.type,
        name=nodetemplate.name,
        title=nodetemplate.name,
        description=nodetemplate.entity_tpl.get("description") or "",
        directives=nodetemplate.directives
    )

    jsonnodetype = types[nodetemplate.type]
    json["properties"] = list(template_properties_to_json(nodetemplate))
    json["dependencies"] = []
    visibility = resource_visibility(spec, nodetemplate)
    if visibility != "inherit":
        json["visibility"] = visibility

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

    ExceptionCollector.start()
    for req in nodetemplate.all_requirements:
        name, req_dict = _get_req(req)
        if name == "dependency":
            continue
        reqDef, rel_template = nodetemplate._get_explicit_relationship(name, req_dict)
        reqconstraint = _find_requirement_constraint(
            jsonnodetype["requirements"], name
        )
        reqjson = dict(
            constraint=reqconstraint, name=name, __typename="Requirement"
        )
        req_metadata = req_dict.get("metadata") or {}
        visibility = req_metadata and "constraint" in req_metadata and req_metadata["constraint"].get("visibility")
        if req_dict.get("node") and not _get_typedef(req_dict["node"], spec):
            # it's not a type, assume it's a node template
            # (the node template name might not match a template if it is only defined in the deployment blueprints)
            match = req_dict["node"]
            reqjson["match"] = match
        if not visibility and reqjson.get("match"):
            # user-defined templates should always have visibility set, so if it doesn't and the requirement is already set to a node
            # assume it is internal and set to hidden.
            if reqconstraint.get("visibility") != "visible" and not req_metadata.get('user_settable'):
                # unless visibility wasn't explicitly set to visibile
                visibility = "hidden"
        if visibility:
            reqjson["constraint"] = dict(reqjson["constraint"], visibility=visibility)
        json["dependencies"].append(reqjson)

    return json


primary_name = "__primary"


def _generate_primary(spec, db, node_tpl=None):
    base_type = node_tpl["type"] if node_tpl else "tosca:Root"
    topology = spec.template.topology_template
    # generate a node type and node template that represents root of the topology
    # generate a type that exposes the topology's inputs and outputs as properties and attributes
    attributes = { # XXX is this computed?
        o.name: dict(default=o.value, type="string", description=o.description or "")
        for o in topology.outputs
    }
    nodetype_tpl = dict(
        derived_from=base_type, properties=topology._tpl_inputs(), attributes=attributes
    )
    # set as requirements all the node templates that aren't the target of any other requirements
    roots = [
        node.toscaEntityTemplate
        for node in spec.nodeTemplates.values()
        if not node.toscaEntityTemplate.get_relationship_templates()
        and "default" not in node.directives
    ]
    nodetype_tpl["requirements"] = [{node.name: dict(node=node.type)} for node in roots]

    topology.custom_defs[primary_name] = nodetype_tpl
    tpl = node_tpl or {}
    tpl["type"] = primary_name
    tpl.setdefault("properties",
    {}).update(
        {name: dict(get_input=name) for name in topology._tpl_inputs()}
    )
    # if create new template, need to assign the nodes explicitly (needed if multiple templates have the same type)
    if not node_tpl:
        tpl["requirements"] = [{node.name: dict(node=node.name)} for node in roots]
    node_spec = spec.add_node_template(primary_name, tpl, False)
    node_template = node_spec.toscaEntityTemplate

    types = db["ResourceType"]
    node_type_to_graphql(
        spec, node_template.type_definition, types
    )
    db["ResourceTemplate"][node_template.name] = nodetemplate_to_json(
        node_template, spec, types
    )
    return node_template


# if a node type or template is specified, use that, but it needs to be compatible with the generated type
def _get_or_make_primary(spec, db):
    ExceptionCollector.start()  # topology.add_template may generate validation exceptions
    topology = spec.template.topology_template
    # we need to generate a root template
    root_type = None
    root = None
    if topology.substitution_mappings:
        if topology.substitution_mappings.node:
            root = topology.node_templates.get(topology.substitution_mappings.node)
            if root:
                root_type = root.type_definition
        else:
            assert topology.substitution_mappings.type
            root_type = NodeType(
                topology.substitution_mappings.type, topology.custom_defs
            )

    if root_type:
        properties_tpl = root_type.get_definition("properties") or {}
        for input in topology.inputs:
            if input.name not in properties_tpl:
                root = _generate_primary(spec, db, root and root.entity_tpl)
                break
        if not root:
            root =  topology.node_templates.get(primary_name)
            if not root:
                properties = {name: dict(get_input=name) for name in topology._tpl_inputs()}
                tpl = dict(type=root_type.type, properties=properties)
                node_spec = spec.add_node_template(primary_name, tpl, False)
                root = node_spec.toscaEntityTemplate
                # XXX connections are missing
                db["ResourceTemplate"][root.name] = nodetemplate_to_json(
                    root, spec, db["ResourceType"]
                )
    else:
        root = _generate_primary(spec, db)

    assert root
    return root.name, root.type


def to_graphql_blueprint(spec, db, deploymentTemplates=None):
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
    title, name = _template_title(spec, root_name)
    blueprint = dict(__typename="ApplicationBlueprint", name=name, title=title)
    blueprint["primary"] = root_type
    blueprint["deploymentTemplates"] = deploymentTemplates or []
    blueprint["description"] = spec.template.description
    metadata = spec.template.tpl.get("metadata") or {}
    blueprint["livePreview"] = metadata.get("livePreview")
    blueprint["sourceCodeUrl"] = metadata.get("sourceCodeUrl")
    blueprint["image"] = metadata.get("image")
    blueprint["projectIcon"] = metadata.get("projectIcon")
    blueprint["primaryDeploymentBlueprint"] = metadata.get("primaryDeploymentBlueprint")
    return blueprint, root_name


def slugify(text):
    # XXX only allow env var names?
    text = text.lower().strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w\-]", "", text)
    return re.sub(r"\-\-+", "-", text)

def _template_title(spec, default):
    metadata = spec.template.tpl.get("metadata") or {}
    name = metadata.get("template_name") or default
    slug = slugify(name)
    title = metadata.get("title") or name
    return title, slug


def get_deployment_blueprints(manifest, blueprint, root_name, db):
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
    }
    """
    deployment_blueprints = (
        manifest.manifest.expanded.get("spec", {}).get("deployment_blueprints") or {}
    )
    spec = manifest.tosca
    deployment_templates = {}
    for name, tpl in deployment_blueprints.items():
        slug = slugify(name)
        template = tpl.copy()
        local_resource_templates = {}
        resource_templates = template.pop("resource_templates", None)
        if resource_templates:
            for node_name, node_tpl in resource_templates.items():
                # nodes here overrides node_templates
                node_spec = spec.add_node_template(node_name, node_tpl, False)
                node_template = node_spec.toscaEntityTemplate
                t = nodetemplate_to_json(
                    node_template, spec, db["ResourceType"]
                )
                if t.get("visibility") == 'omit':
                    continue
                local_resource_templates[node_name] = t

        # XXX assert that db["ResourceTemplate"] has already has all the node_templates
        # names of ResourceTemplates:
        resourceTemplates = list(set(db["ResourceTemplate"]) | set(local_resource_templates))
        template.update(
            dict(
                __typename="DeploymentTemplate",
                title=tpl.get("title") or name,
                description=tpl.get("description"),
                name=name,
                slug=slug,
                blueprint=blueprint["name"],
                primary=tpl.get("primary") or root_name,
                resourceTemplates=resourceTemplates,
                ResourceTemplate=local_resource_templates
            )
        )
        deployment_templates[name] = template
    return deployment_templates


def get_blueprint_from_topology(manifest, db):
    spec = manifest.tosca
    title, slug = _template_title(spec, "unnamed")
    # XXX cloud = spec.topology.primary_provider
    blueprint, root_name = to_graphql_blueprint(spec, db, [title])
    templates = get_deployment_blueprints(manifest, blueprint, root_name, db)
    deployment_name = os.getenv("DEPLOYMENT")
    if deployment_name and deployment_name in templates:
        template = templates[deployment_name].copy()
    else:
        # just use the first one if present
        for name, value in templates.items():
            template = value.copy()
            break
        else:
            # none exist, create one
            template = dict(
                __typename="DeploymentTemplate",
                title=title,
                name=slug,
                slug=slug,
                description=spec.template.description,
                # names of ResourceTemplates
                resourceTemplates=list(db["ResourceTemplate"]),
                ResourceTemplate={}
            )
    template["blueprint"] = blueprint["name"]
    template["primary"] = root_name
    return blueprint, template


def to_graphql(localEnv):
    # set skip_validation because we want to be able to dump incomplete service templates
    manifest = localEnv.get_manifest(skip_validation=True)
    db = {}
    spec = manifest.tosca
    types_repo = spec.template.tpl.get("repositories").get('types')
    if types_repo: # only export types, avoid built-in repositories
        db["repositories"] = {'types': types_repo}
    types = to_graphql_nodetypes(spec)
    db["ResourceType"] = types
    db["ResourceTemplate"] = {}
    environment_instances = {}
    connection_types = {}
    for node_spec in spec.nodeTemplates.values():
        toscaEntityTemplate = node_spec.toscaEntityTemplate
        t = nodetemplate_to_json(toscaEntityTemplate, spec, types)
        if t.get("visibility") == 'omit':
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
    for template in spec.template.relationship_templates:
        if template.default_for:
            type_definition = template.type_definition
            typename = type_definition.type
            if typename in types:
                connection_types[typename] = types[typename]
            elif typename not in connection_types:
                node_type_to_graphql(
                    spec, type_definition, connection_types
                )
            connection_template = nodetemplate_to_json(template, spec, connection_types)
            if connection_template.get("visibility") == 'omit':
                continue
            name = connection_template["name"]
            assert name not in db["ResourceTemplate"], f"template name conflict: {name}"
            # db["ResourceTemplate"][name] = connection_template
            connections[name] = connection_template

    db["Overview"] = manifest.tosca.template.tpl.get("metadata") or {}
    env = dict(
            connections=connections,
            primary_provider=connections.get("primary_provider"),
            instances=environment_instances,
            repositories = manifest.context.get('repositories') or {}
        )
    return db, manifest, env, connection_types


def add_graphql_deployment(manifest, db, dtemplate):
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
    }
    """
    name = dtemplate['name']
    title = dtemplate.get('title') or name
    deployment = dict(name=name, title=title, __typename="Deployment")
    templates = db["ResourceTemplate"]
    relationships = {}
    for t in templates.values():
        if "dependencies" in t:
            for c in t["dependencies"]:
                if c.get("match"):
                    relationships.setdefault(c["match"], []).append(c)
    resources = [
        to_graphql_resource(instance, manifest, db, relationships)
        for instance in manifest.rootResource.get_self_and_descendents()
        if instance is not manifest.rootResource and instance.template.name in templates
    ]
    db["Resource"] = {r["name"]: r for r in resources}
    deployment["resources"] = list(db["Resource"])
    primary_name = deployment["primary"] = dtemplate["primary"]
    deployment['deploymentTemplate'] = dtemplate["name"]
    if manifest.lastJob:
        readyState = manifest.lastJob.get("readyState")
        workflow = manifest.lastJob.get("workflow")
        deployment['workflow'] = workflow
        if isinstance(readyState, dict):
            deployment['status'] = to_enum(Status, readyState.get("effective", readyState.get("local")) )
            if workflow == "undeploy" and deployment['status'] == Status.ok:
                deployment['status'] = Status.absent

        deployment['summary'] = manifest.lastJob.get("summary")
    deployment['ci_job_id'] = os.getenv('CI_JOB_ID')
    deployment['ci_pipeline_id'] = os.getenv('CI_PIPELINE_ID')

    url = manifest.rootResource.attributes["outputs"].get("url")
    primary_resource = db["Resource"].get(primary_name)
    if url:
        deployment["url"] = url
    elif primary_resource and primary_resource.get("attributes"):
        for prop in primary_resource["attributes"]:
            if prop["name"] == "url":
                deployment["url"] = prop["value"]
                break
    db["Deployment"] = {name: deployment}
    return deployment


def to_blueprint(localEnv):
    db, manifest, env, env_types = to_graphql(localEnv)
    blueprint, root_name = to_graphql_blueprint(manifest.tosca, db)
    deployment_blueprints = get_deployment_blueprints(manifest, blueprint, root_name, db)
    db["DeploymentTemplate"] = deployment_blueprints
    blueprint["deploymentTemplates"] = list(deployment_blueprints)
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    return db


# NB! to_deployment is the default export format used by __main__.export (but you won't find that via grep)
def to_deployment(localEnv):
    db, manifest, env, env_types = to_graphql(localEnv)
    blueprint, dtemplate = get_blueprint_from_topology(manifest, db)
    db["DeploymentTemplate"] = {dtemplate["name"]: dtemplate}
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    deployment = add_graphql_deployment(manifest, db, dtemplate)
    # don't include base node type:
    db["ResourceType"].pop("tosca.nodes.Root")
    return db


def mark_leaf_types(types):
    # types without implementations can't be instantiated by users
    # treat non-leaf types as abstract types that shouldn't be instatiated
    # treat leaf types that have requirements as having implementations
    counts = Counter()
    for rt in types.values():
        for name in rt["extends"]:
            counts[name] += 1
    for (name, count) in counts.most_common():
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

def annotate_properties(types):
    annotations = {}
    for jsontype in types.values():
        for req in (jsontype.get("requirements") or []):
            if req.get('node_filter'):
                annotations[req['resourceType']] = req
    for jsontype in types.values():
        for typename in jsontype['extends']:
            node_filter = annotations.get(typename)
            if node_filter:
                map_nodefilter(node_filter, jsontype['inputsSchema']['properties'])

def map_nodefilter(filters, jsonprops):
    ONE_TO_ONE_MAP = dict(object="map", array="list")
    for name, value in get_nodefilters(filters, "properties"):
        if name not in jsonprops or not isinstance(value, dict):
            continue
        if 'eval' in value:
            # the filter declared an expression to set the property's value
            # delete the annotated property from the target to it hide from the user
            # (since they can't shouldn't set this property now)
            del jsonprops[name]
        else:
            # update schema definition with the node filter's constraints
            schema = jsonprops[name]
            tosca_datatype = schema.get("$toscatype") or ONE_TO_ONE_MAP.get(schema['type'], schema['type'])
            constraints = ConditionClause(name, value, tosca_datatype).conditions
            schema.update(map_constraints(schema["type"], constraints, schema))

def to_environments(localEnv):
    """
      Map environments in unfurl.yaml to DeploymentEnvironments
      Map registered ensembles to deployments just with path reference to the ensemble's json

      type DeploymentEnvironment {
        name: String!
        connections: [ResourceTemplate!]
        instances: [ResourceTemplate!]

        primary_provider: ResourceTemplate
        deployments: [String!]
      }

    # save service templates in this format so we can include this json in unfurl.yaml
    {
      "DeploymentEnvironment": {
        name: {
          "connections": { name: <ResourceTemplate>},
          "instances": { name: <ResourceTemplate>}
        }
      },

      "ResourceType": {
        name: <ResourceType>
      },
    }
    """

    # XXX one manifest and blueprint per environment
    environments = {}
    all_connection_types = {}
    blueprintdb = None
    for name in localEnv.project.contexts:
        # we create new LocalEnv for each context because we need to instantiate a different ToscaSpec object
        blueprintdb, manifest, env, env_types  = to_graphql(
            LocalEnv(
                localEnv.manifestPath,
                project=localEnv.project,
                override_context=name,
            )
        )
        env['name'] = name
        environments[name] = env
        all_connection_types.update(env_types)

    db = {}
    db["DeploymentEnvironment"] = environments
    if blueprintdb:
        if blueprintdb.get("repositories", {}).get("types"):
            # don't include ResourceTypes if we are including a types repository
            db["ResourceType"] = {}
            return db

        # add the rest of the types too
        # XXX is it safe to only include types with "connect" implementations?
        all_connection_types.update(blueprintdb["ResourceType"])
    db["ResourceType"] = all_connection_types
    return db

def add_attributes(instance):
    attrs = []
    attributeDefs = instance.template.attributeDefs.copy()
    # instance._attributes only has values that were set by the instance, not spec properties or attribute defaults
    # instance._attributes should already be serialized
    if instance._attributes:
        for name, value in instance._attributes.items():
            p = attributeDefs.pop(name, None)
            if not p:
                # check if shadowed property
                p = instance.template.propertyDefs.get(name)
                if not p:
                    # same as EntityTemplate._create_properties()
                    p = Property(name, value, dict(type="any"),
                            instance.template.toscaEntityTemplate.custom_def)
            if not p.schema.get("metadata", {}).get("internal"):
                attrs.append(dict(name=p.name, value=attribute_value_to_json(p, value)))
    # add leftover attribute defs that have a default value
    for prop in attributeDefs.values():
        if prop.default is not None and not is_computed(prop):
            attrs.append( dict(name=prop.name, value=attribute_value_to_json(prop, prop.default)) )
    return attrs


def add_computed_properties(instance):
    attrs = []
    _attributes = instance._attributes or {}
    # instance._properties should already be serialized
    if instance._properties:
        for name, value in instance._properties.items():
            if name in _attributes: # shadowed, don't include both
                continue
            p = instance.template.propertyDefs.get(name)
            if not p:
                # same as EntityTemplate._create_properties()
                p = Property(name, value, dict(type="any"),
                        instance.template.toscaEntityTemplate.custom_def)
            if p.schema.get("metadata", {}).get("visibility") != 'hidden':
                if isinstance(value, sensitive) and _is_get_env_or_secret(p.value):
                    value = p.value
                else:
                    value = attribute_value_to_json(p, value)
                attrs.append(dict(name=p.name, value=value))
    return attrs


def to_graphql_resource(instance, manifest, db, relationships):
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
    }
    """
    # XXX url
    template = db["ResourceTemplate"].get(instance.template.name)
    assert template # pre-condition
    resource = dict(
        name=instance.name,
        title=template.get("title", instance.name),
        template=instance.template.name,
        state=instance.state,
        status=instance.status,
        __typename="Resource",
    )
    if "visibility" in template and template["visibility"] != "inherit":
        resource["visibility"] = template["visibility"]
    elif instance.template.name in relationships:
        visibilities = set(reqjson["constraint"].get("visibility", "inherit")
                            for reqjson in relationships[instance.template.name])
        if len(visibilities) == 1 and "hidden" in visibilities:
            # non-visible visibilities override hidden
            resource["visibility"] = "hidden"
    resource["protected"] = instance.protected
    resource["attributes"] = add_attributes(instance)
    resource["computedProperties"] = add_computed_properties(instance)

    if template["dependencies"]:
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
