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
from toscaparser.activities import ConditionClause
from .tosca import is_function, get_nodefilters
from .localenv import LocalEnv

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
    any={"type": "string"},  # we'll have to interpret the string as json
    range={"type": "array", "items": {"type": "integer", "maxItems": 2}},
    PortDef={"type": "number", "minimum": 1, "maximum": 65535}
    # XXX:
    # PortSpec.SHORTNAME,
)

_SCALAR_TYPE = {}
VALUE_TYPES.update(
    {
        "scalar-unit.size": _SCALAR_TYPE,
        "scalar-unit.frequency": _SCALAR_TYPE,
        "scalar-unit.time": _SCALAR_TYPE,
        # "scalar-unit.bitrate": _SCALAR_TYPE, # XXX add parser support 3.3.6.7 scalar-unit.bitrate
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
        schema.update(map_constraints(schema["type"], toscaSchema.constraints))
    return schema


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
    else:
        assert isinstance(req, dict), f"bad {req} in {req_dict}"
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
        # req['node'] can be a node_template instead of a type
        if nodetype in spec.nodeTemplates:
            nodetype = spec.nodeTemplates[nodetype].type
    else:
        nodetype = req["capability"]
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
        types[name] = {}
        types[name] = node_type_to_graphql(spec, typedef, types)
    for p in typedef.parent_types():
        _get_extends(spec, p, extends, types)


def is_resource_user_visible(spec, t):
  if 'default' in t.directives:
      return False
  metadata = t.entity_tpl.get('metadata')
  if metadata and metadata.get('internal'):
      return False
  if spec.discovered and t.name in spec.discovered:
      return False
  return True


def is_property_user_visible(p):
    if p.schema.get("metadata", {}).get("user_settable"):
        return True
    if p.default is not None or is_computed(p):
        return False
    return True


def is_computed(p):
    # XXX be smarter about is_computed() if the user should be able to override the default
    return (
        p.name in ["tosca_id", "state", "tosca_name"]
        or is_function(p.value)
        or is_function(p.default)
        or p.schema.get("metadata", {}).get("computed")
    )


def property_value_to_json(p):
    scalar_class = get_scalarunit_class(p.type)
    if scalar_class:
        unit = p.schema.metadata.get("default_unit")
        if unit:  # covert to the default_unit
            return scalar_class(p.value).get_num_from_scalar_unit(unit)
    return p.value


def _include_requirement(spec, req):
    name = next(iter(req))
    if name == "dependency":
        return False
    if isinstance(req[name], str):
        node = req[name]
    else:
        node = req[name].get("node")
    if node and node in spec.nodeTemplates:
        # using a predefined template, so hide this requirement from the user
        metadata = spec.nodeTemplates[node].toscaEntityTemplate.entity_tpl.setdefault('metadata', {})
        if 'internal' not in metadata:
            # hide this template from the user
            metadata['internal'] = True
        return False
    return True


# XXX outputs: only include "public" attributes?
def node_type_to_graphql(spec, type_definition, types: dict):
    """
    type ResourceType {
      name: string!
      title: string
      extends: [ResourceType!]
      description: string
      badge: string
      inputsSchema: JSON
      outputsSchema: JSON
      requirements: [RequirementConstraint!]
      implementations: [string]
      implementation_requirements: [string]
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
        if "details_url" in metadata:
            jsontype["details_url"] = metadata["details_url"]

    propertydefs = (
        p for p in type_definition.get_properties_def_objects() if is_property_user_visible(p)
    )
    jsontype["inputsSchema"] = tosca_type_to_jsonschema(spec, propertydefs, None)

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

    jsontype["requirements"] = [
        requirement_to_graphql(spec, req)
        for req in type_definition.get_all_requirements()
        if _include_requirement(spec, req)
    ]

    operations = set(
        op.name for op in EntityTemplate._create_interfaces(type_definition, None)
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
        typedef = _make_typedef(typename, custom_defs)
        if typedef:
            types[typename] = node_type_to_graphql(spec, typedef, types)
    for typename in StatefulEntityType.TOSCA_DEF: # builtin types
        if typename.startswith('unfurl.nodes') or typename.startswith('unfurl.relationships'):
            # only include our extensions
            typedef = _make_typedef(typename, custom_defs)
            if typedef:
                types[typename] = node_type_to_graphql(spec, typedef, types)

    for node_spec in spec.nodeTemplates.values():
        type_definition = node_spec.toscaEntityTemplate.type_definition
        typename = type_definition.type
        if typename not in types:
            types[typename] = {}  # set now to avoid circular references
            types[typename] = node_type_to_graphql(spec, type_definition, types)

    mark_user_visible(types)
    annotate_properties(types)
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


def nodetemplate_to_json(nodetemplate, spec, types):
    """
    Returns json object as a ResourceTemplate:

    type ResourceTemplate {
      name: String!
      title: String
      type: ResourceType!

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
    json = dict(
        type=nodetemplate.type,
        name=nodetemplate.name,
        title=nodetemplate.name,
        description=nodetemplate.entity_tpl.get("description") or "",
    )

    jsonnodetype = types[nodetemplate.type]
    json["properties"] = [
        dict(name=p.name, value=property_value_to_json(p))
        for p in nodetemplate.get_properties_objects()
        if is_property_user_visible(p)
    ]
    json["dependencies"] = []

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
    if nodetemplate.requirements:
        for req in nodetemplate.requirements:
            reqDef, rel_template = nodetemplate._get_explicit_relationship(req)
            name = next(iter(req))  # first key
            reqconstraint = _find_requirement_constraint(
                jsonnodetype["requirements"], name
            )
            reqjson = dict(
                constraint=reqconstraint, name=name, __typename="Requirement"
            )
            if rel_template and rel_template.target:
                reqjson["match"] = rel_template.target.name
            else:
                reqjson["match"] = None
            json["dependencies"].append(reqjson)

    return json


primary_name = "__primary"


def _generate_primary(spec, db, node_tpl=None):
    base_type = node_tpl["type"] if node_tpl else "tosca:Root"
    topology = spec.template.topology_template
    # generate a node type and node template that represents root of the topology
    # generate a type that exposes the topology's inputs and outputs as properties and attributes
    attributes = {
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
    tpl.setdefault("properties", {}).update(
        {name: dict(get_input=name) for name in topology._tpl_inputs()}
    )
    # if create new template, need to assign the nodes explicitly (needed if multiple templates have the same type)
    if not node_tpl:
        tpl["requirements"] = [{node.name: dict(node=node.name)} for node in roots]
    node_template = topology.add_template(primary_name, tpl)

    types = db["ResourceType"]
    types[primary_name] = node_type_to_graphql(
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
                root = topology.add_template(primary_name, tpl)
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
      deploymentTemplates: [DeploymentTemplate!]

      livePreview: String
      sourceCodeUrl: String
      image: String
      projectIcon: String
    }
    """
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
    return blueprint, root_name


def slugify(text):
    # XXX match unfurl-gui algorithms and only allow env var names
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


# XXX cloud = spec.topology.primary_provider
def get_blueprints_from_topology(manifest, db):
    """
    Returns json object as DeploymentTemplate:

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
    spec = manifest.tosca
    title, slug = _template_title(spec, "unnamed")
    # XXX cloud = spec.topology.primary_provider
    blueprint, root_name = to_graphql_blueprint(spec, db, [title])
    templates = get_deployment_blueprints(manifest, blueprint, root_name)
    for name, value in templates.items():
        template = value
        break
    else:
        template = dict(
            __typename="DeploymentTemplate",
            title=title,
            name=slug,
            slug=slug,
            description=spec.template.description,
        )
    template["blueprint"] = blueprint["name"]
    template["primary"] = root_name
    # names of ResourceTemplates
    template["resourceTemplates"] = list(db["ResourceTemplate"])
    return blueprint, template


def to_graphql(localEnv):
    # set skip_validation because we want to be able to dump incomplete service templates
    manifest = localEnv.get_manifest(skip_validation=True)
    db = {}
    spec = manifest.tosca
    types = to_graphql_nodetypes(spec)
    db["ResourceType"] = types
    db["ResourceTemplate"] = {}
    environment_instances = {}
    connection_types = {}
    for node_spec in spec.nodeTemplates.values():
        toscaEntityTemplate = node_spec.toscaEntityTemplate
        if not is_resource_user_visible(spec, toscaEntityTemplate):
            continue
        t = nodetemplate_to_json(toscaEntityTemplate, spec, types)
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
                connection_types[typename] = {}  # set now to avoid circular references
                connection_types[typename] = node_type_to_graphql(
                    spec, type_definition, connection_types
                )
            connection_template = nodetemplate_to_json(template, spec, connection_types)
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
      job: Job?
      ready: Boolean
      deploymentTemplate: DeploymentTemplate!
      sourceDeploymentTemplateName: String
    }
    """
    # XXX add env deployment graphql needs pipeline metadata (pipeline id, commit, branch)
    # XXX job
    title = dtemplate["name"]
    deployment = dict(name=title, title=title)
    templates = db["ResourceTemplate"]
    deployment["resources"] = [
        to_graphql_resource(instance, manifest, db)
        for instance in manifest.rootResource.get_self_and_descendents()
        if instance is not manifest.rootResource and instance.template.name in templates
    ]
    db["Resource"] = {r["name"]: r for r in deployment["resources"]}
    deployment["primary"] = dtemplate["primary"]
    deployment['deploymentTemplate'] = dtemplate["name"]
    db["Deployment"] = {title: deployment}
    return deployment


def get_deployment_blueprints(manifest, blueprint, root_name):
    deployment_blueprints = (
        manifest.manifest.expanded.get("spec", {}).get("deployment_blueprints") or {}
    )
    deployment_templates = {}
    for name, tpl in deployment_blueprints.items():
        # note: root_resource_template is derived from inputs, outputs and substitution_template from topology_template
        slug = slugify(name)
        template = tpl.copy()
        template.update(
            dict(
                __typename="DeploymentTemplate",
                title=tpl.get("title") or name,
                name=name,
                slug=slug,
                blueprint=blueprint["name"],
                primary=tpl.get("primary") or root_name,
            )
        )
        deployment_templates[name] = template
    return deployment_templates


def to_blueprint(localEnv):
    """
    The blueprint will include deployement blueprints that follows this syntax in the manifest:

    .. code-block:: YAML

      spec:
        deployment_blueprints:
         title: Google Cloud Platform
         cloud: unfurl.relationships.ConnectsTo.GoogleCloudProject
         description: Deploy compute instances on Google Cloud Platform
         resourceTemplates: [resource_template_name*]
        resource_templates:
          <map of ResourceTemplates>
    """
    db, manifest, env, env_types = to_graphql(localEnv)
    blueprint, root_name = to_graphql_blueprint(manifest.tosca, db)
    deployment_blueprints = get_deployment_blueprints(manifest, blueprint, root_name)
    db["DeploymentTemplate"] = deployment_blueprints
    blueprint["deploymentTemplates"] = list(deployment_blueprints)
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    return db


def to_deployment(localEnv):
    db, manifest, env, env_types = to_graphql(localEnv)
    blueprint, dtemplate = get_blueprints_from_topology(manifest, db)
    db["DeploymentTemplate"] = {dtemplate["name"]: dtemplate}
    db["ApplicationBlueprint"] = {blueprint["name"]: blueprint}
    deployment = add_graphql_deployment(manifest, db, dtemplate)
    # don't include base node type:
    db["ResourceType"].pop("tosca.nodes.Root")
    return db


def mark_user_visible(types):
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
                annotations[req['resourceType']] = req['node_filter']
    for jsontype in types.values():
        for typename in jsontype['extends']:
            node_filter = annotations.get(typename)
            if node_filter:
                map_nodefilter(node_filter, jsontype['inputsSchema']['properties'])

def map_nodefilter(filters, jsonprops):
    ONE_TO_ONE_MAP = dict(object="map", array="list")
    for name, value in get_nodefilters(filters):
        if name not in jsonprops or not isinstance(value, dict):
            continue
        if 'eval' in value:
            # delete annotated properties from target
            del jsonprops[name]
        else:
            # update schema definition with node filter constraints
            schema = jsonprops[name]
            tosca_datatype = schema.get("$toscatype") or ONE_TO_ONE_MAP.get(schema['type'], schema['type'])
            constraints = ConditionClause(name, value, tosca_datatype).conditions
            schema.update(map_constraints(schema["type"], constraints))

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
        # add the rest of the types too
        # XXX is it safe to only include types with "connect" implementations?
        all_connection_types.update(blueprintdb["ResourceType"])
    db["ResourceType"] = all_connection_types
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
        name=instance.name,
        title=instance.name,
        template=instance.template.name,
        state=instance.state,
        status=instance.status,
    )
    # instance._attributes only values that were set by the instance, not spec properties or attribute defaults
    # instance._attributes should already be serialized
    template = db["ResourceTemplate"][instance.template.name]
    if instance._attributes:
        outputs = []
        # include the default values by starting with the template's outputs
        if template.get("outputs"):
            outputs = [p.copy() for p in template["outputs"]]
        attributes = instance._attributes.copy()
        for prop in outputs:
            name = prop["name"]
            if name in attributes:
                prop["value"] = attributes.pop(name)
        # left over
        for name, value in attributes.items():
            outputs.append(dict(name=name, value=value))
        resource["attributes"] = outputs
    else:
        resource["attributes"] = []

    if template["dependencies"]:
        requirements = {r["name"]: r.copy() for r in template["dependencies"]}
        for rel in instance.requirements:
            requirements[rel.name]["target"] = rel.target.name
        resource["connections"] = list(requirements.values())
    else:
        resource["connections"] = []
    return resource
