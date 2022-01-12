# Copyright (c) 2022 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Output a normalized json representation of a TOSCA service template for machine consumption.

* TOSCA datatypes are mapped to JSON Schema type definitions
* Node types are flattened to include their parent type's properties, capabilities, and requirements.
* Capabilities are directly represented, instead:
- Node types have a "implements" property that lists all the inherited types and capabilities associated with that type.
- Capability properties are represented as complex properties on the node type (and node template).
- Requirements has a "resourceType" which can be either a node type or a capability type.
"""
import re
from toscaparser.properties import Property
from toscaparser.elements.constraints import Schema
from toscaparser.elements.property_definition import PropertyDef
from toscaparser.elements.nodetype import NodeType
from toscaparser.common.exception import ExceptionCollector

from toscaparser.elements.datatype import DataType
from .tosca import is_function

numeric_constraints = {
    "greater_than": "exclusiveMinimum",
    "greater_or_equal": "minimum",
    "less_than": "exclusiveMaximum",
    "less_or_equal": "maximum",
}


def map_constraint(jsonType, constraint):
    key = constraint.constraint_key
    value = constraint.constraint_value
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
        suffix = dict(string="Length", object="Properties", array="Items")[jsonType]
        prefix = key[:3]
        if prefix == "min" or prefix == "max":
            return {prefix + suffix: value}
        elif key == "length":
            return {"min" + suffix: value, "max" + suffix: value}
        else:  # in_range
            return {"min" + suffix: value[0], "max" + suffix: value[1]}


def map_constraints(jsonType, constraints):
    if len(constraints) > 1:
        return dict(allOf=[map_constraint(jsonType, c) for c in constraints])
    else:
        return map_constraint(jsonType, constraints[0])


ONE_TO_ONE_TYPES = ("string", "boolean", "number", "map", "list")

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
    any={"type": "string"},  # we'll have to interpret the string as json
    range={"type": "array", "items": {"type": "integer", "maxItems": 2}},
)

_SCALAR_TYPE = {
    "type": "object",
    "properties": {"value": {"type": "number"}, "unit": {"type": "string"}},
}
VALUE_TYPES.update(
    {
        "scalar-unit.size": _SCALAR_TYPE,
        "scalar-unit.frequency": _SCALAR_TYPE,
        "scalar-unit.time": _SCALAR_TYPE,
        # XXX:
        # "version",
        # "PortDef",
        # PortSpec.SHORTNAME,
    }
)


def tosca_type_to_jsonschema(dt, spec, propdefs):
    jsonschema = dict(
        type="object",
        properties={p.name: tosca_schema_to_jsonschema(p, spec) for p in propdefs},
    )
    # add typeid:
    jsonschema["properties"].update({"$toscatype": dict(const=dt.type)})
    return jsonschema


def get_valuetype(dt):
    schema = Schema(None, dt.defs)
    while dt.value_type not in VALUE_TYPES:
        dt = dt.parent_type()
    return VALUE_TYPES.get(dt.value_type), schema


def tosca_schema_to_jsonschema(p, spec, omitComputed=True):
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
    if p.name:
        schema["title"] = p.name
    if toscaSchema.default is not None:
        if is_function(toscaSchema.default) and omitComputed:
            return None
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
        type_schema = VALUE_TYPES[tosca_type]
    else:
        dt = DataType(tosca_type, spec.template.topology_template.custom_defs)
        if dt.value_type:  # its a simple type
            type_schema, valuetype_schema = get_valuetype(dt)
            if valuetype_schema.constraints:  # merge constraints
                constraints = valuetype_schema.constraints + constraints
        else:  # it's complex type with properties:
            type_schema = tosca_type_to_jsonschema(
                dt, spec, dt.get_properties_def_objects()
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
        schema.update(map_constraints(schema["type"], toscaSchema.constraints))
    return schema


def propertydefs_to_jsonschema(spec, pdefs):
    return list(
        filter(
            None,
            [
                tosca_schema_to_jsonschema(p, spec)
                for p in pdefs
                if p.name not in ["tosca_id", "state", "tosca_name"]
            ],
        )
    )


def requirement_to_graphql(spec, req_dict):
    """
    type RequirementConstraint {
        name: String!
        title: String
        description: String
        resourceType: ResourceType!
        min: Int
        max: Int
        badge: String
    }
    """
    name, req = list(req_dict.items())[0]
    if isinstance(req, str):
        req = dict(node=req)
    reqobj = dict(name=name, title=name, description=req.get("description") or "")
    metadata = req.get("metadata")
    if metadata:
        if "badge" in metadata:
            reqobj["badge"] = metadata["badge"]
        if "title" in metadata:
            reqobj["title"] = metadata["title"]

    if "occurrences" in req:
        reqobj["min"] = req["occurrences"][0]
        reqobj["max"] = req["occurrences"][1]
    else:
        reqobj["min"] = 1
        reqobj["max"] = 1

    nodetype = req.get("node")
    if nodetype:
        if nodetype in spec.template.topology_template.nodetemplates:
            # req['node'] can be a node_template instead of a type
            nodetype = spec.template.topology_template.nodetemplates[
                nodetype
            ].type_definition.type
    else:
        nodetype = req["capability"]
    reqobj["resourceType"] = nodetype

    return reqobj


def _get_interfaces(spec, typedef, implements: list, types):
    if not typedef:
        return
    name = typedef.type
    if typedef.type not in implements:
        implements.append(name)
    if types is not None and name not in types:
        types[name] = {}
        types[name] = node_type_to_graphql(spec, typedef, types)
    for p in typedef.parent_types():
        _get_interfaces(spec, p, implements, types)


# XXX outputs: only include "public" attributes?
def node_type_to_graphql(spec, type_definition, types: dict):
    """
    type ResourceType {
      name: string!
      title: string
      implements: [ResourceType!]
      description: string
      badge: string
      properties: [Input!]
      outputs: [Output!]
      requirements: [RequirementConstraint!]
    }
    """
    jsontype = dict(
        name=type_definition.type,  # fully qualified name
        title=type_definition.type.split(".")[-1],  # short, readable name
        description=type_definition.get_value("description") or "",
    )
    metadata = type_definition.get_value("metadata")
    if metadata:
        if "badge" in metadata:
            jsontype["badge"] = metadata["badge"]
        if "title" in metadata:
            jsontype["title"] = metadata["title"]

    implements = []
    # add ancestors classes to implements
    _get_interfaces(spec, type_definition, implements, types)
    # add capabilities types to implements
    for cap in type_definition.get_capability_typedefs():
        _get_interfaces(spec, cap, implements, None)
    jsontype["implements"] = implements

    jsontype["properties"] = propertydefs_to_jsonschema(
        spec, type_definition.get_properties_def_objects()
    )

    # treat each capability as a complex property
    add_capabilities_as_properties(jsontype["properties"], type_definition, spec)

    # XXX only include "public" attributes?
    jsontype["outputs"] = propertydefs_to_jsonschema(
        spec, type_definition.get_attributes_def_objects()
    )

    # XXX capabilities can hava attributes too
    add_capabilities_as_attributes(jsontype["outputs"], type_definition, spec)

    jsontype["requirements"] = [
        requirement_to_graphql(spec, req)
        for req in (type_definition.get_all_requirements() or ())
        if next(iter(req)) != "dependency"
    ]

    return jsontype


def to_graphql_nodetypes(spec):
    # node types are readonly, so mapping doesn't need to be bijective
    types = {}
    custom_defs = spec.template.topology_template.custom_defs
    for typename, defs in custom_defs.items():
        for key in defs:
            # hacky way to avoid validation exception if type isn't a node type
            if key not in NodeType.SECTIONS:
                break
        else:
            typedef = NodeType(typename, custom_defs)
            if typedef.is_derived_from("tosca.nodes.Root"):
                types[typename] = node_type_to_graphql(spec, typedef, types)

    for toscaNodeTemplate in spec.template.topology_template.nodetemplates:
        type_definition = toscaNodeTemplate.type_definition
        typename = type_definition.type
        if typename not in types:
            types[typename] = {}  # set now to avoid circular references
            types[typename] = node_type_to_graphql(spec, type_definition, types)
    return types


def deduce_valuetype(value):
    typemap = {
        int: "number",
        float: "number",
        type(None): "null",
        dict: "object",
        list: "array",
    }
    for pythontype in typemap:
        if isinstance(value, pythontype):
            return dict(type=typemap[pythontype])
    return {}


def is_computed(p):
    return (
        p.name in ["tosca_id", "state", "tosca_name"]
        or is_function(p.value)
        or is_function(p.default)
    )


def deduce_prop(name, value):
    # additional property not defined in the node type
    jsonprop = deduce_valuetype(value)
    jsonprop["title"] = name
    jsonprop["value"] = value
    return jsonprop


def get_properties(typedefs, props):
    """
    Return a JSON-Schema object that can also be used as a Input GraphQL type.
    Extra properties include: value, title, description, default, required, $toscatype
    """
    jsonprops = []
    for propdef in typedefs:
        # find the property with the given name
        # note: the property won't be defined it if propdef is a capability
        prop = props.pop(propdef["title"], None)
        jsonprop = propdef.copy()
        if prop:
            jsonprop["value"] = prop.value
        jsonprops.append(jsonprop)

    # look at leftover props
    for prop in props.values():
        if not is_computed(prop):
            jsonprops.append(deduce_prop(prop.name, prop.value))
    return jsonprops


def add_capabilities_as_properties(props, nodetype, spec):
    """treat each capability as a property and add them to props"""
    for cap in nodetype.get_capability_typedefs():
        if not cap.get_definition("properties"):
            continue
        schema = tosca_type_to_jsonschema(cap, spec, cap.get_properties_def_objects())
        schema["title"] = cap.name
        props.append(schema)


def add_capabilities_as_attributes(props, nodetype, spec):
    """treat each capability as a property and add them to props"""
    for cap in nodetype.get_capability_typedefs():
        if not cap.get_definition("attributes"):
            continue
        schema = tosca_type_to_jsonschema(cap, spec, cap.get_attributes_def_objects())
        schema["title"] = cap.name
        props.append(schema)


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


def nodetemplate_to_json(nodetemplate, spec, db):
    """
    Returns json object as a ResourceTemplate:

    type ResourceTemplate {
      name: String!
      title: String
      type: ResourceType!

      description: string

      # input has schema and value
      properties: [Input!]

      requirements: [Requirement!]
    }

    type Requirement {
      name: String
      constraint: RequirementConstraint!
      match: ResourceTemplate
      target: Resource
    }
    """
    types = db["ResourceType"]
    json = dict(
        type=nodetemplate.type,
        name=nodetemplate.name,
        title=nodetemplate.name,
        description=nodetemplate.entity_tpl.get("description") or "",
        __typename="ResourceTemplate",
    )

    jsonnodetype = types[nodetemplate.type]
    json["properties"] = get_properties(
        jsonnodetype["properties"], nodetemplate.get_properties()
    )

    # treat each capability as a complex property
    capabilities = nodetemplate.get_capabilities()
    for prop in json["properties"]:
        # properties are already added via the type definition
        cap = capabilities.get(prop["title"])
        if cap:
            prop["value"] = cap._properties

    # this is the same as on the type because of the bug where attribute set on a node_template are ignored
    json["outputs"] = jsonnodetype["outputs"]

    json["requirements"] = []
    ExceptionCollector.start()
    if nodetemplate.requirements:
        for req in nodetemplate.requirements:
            reqDef, rel_template = nodetemplate._get_explicit_relationship(req)
            name, tpl = next(iter(req.items()))
            reqconstraint = _find_requirement_constraint(
                jsonnodetype["requirements"], name
            )
            reqjson = dict(constraint=reqconstraint, name=name, __typename="Requirement")
            if rel_template and rel_template.target:
                reqjson["match"] = rel_template.target.name
            else:
                reqjson["match"] = None
            json["requirements"].append(reqjson)

    return json


def to_graphql_blueprint(type, spec, deploymentTemplates=None):
    """
    Returns json object as ApplicationBlueprint

    ApplicationBlueprint: {
      name: String!
      primary: ResourceType!
      deploymentTemplates: [DeploymentTemplate!]
    }
    """
    name = spec.template.tpl.get("template_name") or ""
    blueprint = dict(__typename="ApplicationBlueprint", name=name)
    blueprint["primary"] = type
    blueprint["deploymentTemplates"] = deploymentTemplates or []
    return blueprint


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w\-]", "", text)
    return re.sub(r"\-\-+", "-", text)


# XXX cloud = spec.topology.primary_provider
# XXX substitution_template
def to_graphql_deployment_template(manifest, db):
    """
    Returns json object as DeploymentTemplate:

    type DeploymentTemplate {
      title: String!
      slug: String!
      description: String

      blueprint: ApplicationBlueprint!
      primary: ResourceTemplate!
      resourceTemplates: [ResourceTemplate!]

      # XXX:
      cloud: ResourceType
    }
    """
    spec = manifest.tosca
    title = spec.template.tpl.get("template_name") or "unnamed"
    slug = slugify(title)
    template = dict(
        __typename="DeploymentTemplate",
        title=title,
        slug=slug,
        description=spec.template.description,
    )
    # XXX cloud = spec.topology.primary_provider
    topology = spec.template.topology_template
    if topology.substitution_mappings and topology.substitution_mappings.node:
        root = topology.node_templates[topology.substitution_mappings.node]
    else:
        root = list(topology.nodetemplates)[0]
    blueprint = to_graphql_blueprint(root.type, spec, [title])
    template["blueprint"] = blueprint["name"]
    template["primary"] = root.name
    # names of ResourceTemplates
    template["resourceTemplates"] = list(db["ResourceTemplate"])
    return blueprint, template


def to_graphql(manifest):
    db = {}
    spec = manifest.tosca
    db["ResourceType"] = to_graphql_nodetypes(spec)
    db["ResourceTemplate"] = {
        t["name"]: t
        for t in [
            nodetemplate_to_json(toscaNodeTemplate, spec, db)
            for toscaNodeTemplate in spec.template.topology_template.nodetemplates
        ]
    }
    blueprint, dtemplate = to_graphql_deployment_template(manifest, db)
    db["DeploymentTemplate"] = {dtemplate["title"]: dtemplate}
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    db["Overview"] = spec.template.tpl.get("metadata") or {}
    add_graphql_deployment(manifest, db, dtemplate)
    # don't include base node type:
    db["ResourceType"].pop("tosca.nodes.Root")
    return db


def add_graphql_deployment(manifest, db, dtemplate):
    """
    type Deployment {
      title: String!
      primary: Resource
      resources: [Resource!]
      job: Job?
      ready: Boolean
      deploymentTemplate: DeploymentTemplate!
      sourceDeploymentTemplateName: String
    }
    """
    title = "unnamed"  # XXX
    deployment = dict(title=title)
    # XXX primary
    deployment["resources"] = [
        to_graphql_resource(instance, manifest, db)
        for instance in manifest.rootResource.get_self_and_descendents()
        if instance is not manifest.rootResource
    ]
    db["Resource"] = {r["name"]: r for r in deployment["resources"]}
    deployment["primary"] = dtemplate['primary']
    db["Deployment"] = {title: deployment}
    return db


def to_graphql_resource(instance, manifest, db):
    """
    type Resource {
      name: String!
      title: String!
      url: String
      template: ResourceTemplate!
      status: Status
      state: State
      attributes: [Input!]
      connections: [Requirement!]
    }
    """
    # XXX url
    resource = dict(
        name=instance.key,
        title=instance.name,
        template=instance.template.name,
        state=instance.state,
        status=instance.status,
    )
    # only save the attributes that were set by the instance, not spec properties or attribute defaults
    # _attributes should already be serialized
    template = db["ResourceTemplate"][instance.template.name]
    if instance._attributes:
        inputs = []
        if template["outputs"]:
            inputs = [p.copy() for p in template["outputs"]]
        attributes = instance._attributes.copy()
        for prop in inputs:
            name = prop["title"]
            if name in attributes:
                prop["value"] = attributes.pop(name)
        for name, value in attributes.items():  # left over
            inputs.append(deduce_prop(name, value))
        resource["attributes"] = inputs
    else:
        resource["attributes"] = []

    if template["requirements"]:
        requirements = {r["name"]: r.copy() for r in template["requirements"]}
        for rel in instance.requirements:
            requirements[rel.name]["target"] = rel.target.key
        resource["connections"] = list(requirements.values())
    else:
        resource["connections"] = []
    return resource
