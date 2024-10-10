from typing import Any, Dict, List, Optional, Tuple, cast
import sys
from .tosca_solver import (  # type: ignore
    solve,
    CriteriaTerm,
    Field,
    FieldValue,
    ToscaValue,
    SimpleValue,
    Constraint,
    QueryType,
)
from tosca.yaml2python import has_function
from toscaparser.elements.relationshiptype import RelationshipType
from toscaparser.elements.scalarunit import get_scalarunit_class, ScalarUnit
from toscaparser.elements.constraints import Schema, Constraint as ToscaConstraint
from toscaparser.properties import Property
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.topology_template import TopologyTemplate
from .eval import Ref, analyze_expr
from .logs import getLogger

logger = getLogger("unfurl")

Solution = Dict[Tuple[str, str], List[Tuple[str, str]]]


class Node:
    def __init__(self, name, type="tosca.nodes.Root", fields=None):
        self.name: str = name
        self.tosca_type: str = type
        self.fields: List[Field] = fields or []
        self.has_restrictions: bool = False

    def __repr__(self) -> str:
        return f"Node({self.name}, {self.tosca_type}, {self.has_restrictions}, {self.fields!r})"


def deduce_type(value):
    ctor = None
    schema: Optional[dict] = None
    for py_type, toscatype in Schema.PYTHON_TO_PROPERTY_TYPES.items():
        if isinstance(value, py_type):
            ctor = getattr(SimpleValue, toscatype, None)
            if ctor:
                schema = dict(type=toscatype)
                if toscatype == "map":
                    if not value:
                        schema["entry_schema"] = dict(type="string")
                    else:
                        _, schema["entry_schema"] = deduce_type(
                            next(iter(value.values()))
                        )
                elif toscatype == "list":
                    if not value:
                        schema["entry_schema"] = dict(type="string")
                    else:
                        _, schema["entry_schema"] = deduce_type(value[0])
    return ctor, schema


def include_value(v):
    return v is not None and not has_function(v)


def tosca_to_rust(prop: Property) -> ToscaValue:
    value = prop.value
    schema = prop.schema
    entity = prop.entry_schema_entity or prop.entity

    tosca_type = schema.type
    if tosca_type == "string" and not isinstance(value, str):
        tosca_type = "any"
    ctor = getattr(SimpleValue, tosca_type, None)
    if ctor:
        typename = None
    else:
        typename = tosca_type
        if tosca_type in ["version", "timestamp"]:
            ctor, any_schema_dict = deduce_type(value)
        elif tosca_type.startswith("scalar-unit."):
            scalar_class = get_scalarunit_class(tosca_type)
            assert scalar_class, tosca_type
            value = scalar_class(value).get_num_from_scalar_unit()
            ctor = SimpleValue.float
        elif tosca_type == "number":
            ctor = SimpleValue.float
        elif tosca_type in ["tosca.datatypes.network.PortDef", "PortDef"]:
            ctor = SimpleValue.integer
        elif tosca_type in ["tosca.datatypes.network.PortSpec", "PortSpec"]:
            ctor = SimpleValue.map
        elif tosca_type == "any":
            ctor, any_schema_dict = deduce_type(value)
            tosca_type = any_schema_dict["type"]
            schema = Schema(prop.name, any_schema_dict)
        elif entity:  # find simple type from complex type
            # use a TOSCA datatype
            if entity.datatype.value_type:  # its a simple value type
                tosca_type = entity.datatype.value_type
                ctor = getattr(SimpleValue, tosca_type, None)
            else:
                value = {
                    name: tosca_to_rust(prop)
                    for name, prop in entity.properties.items()
                    if include_value(prop.value)
                }
                return ToscaValue(SimpleValue.map(value), typename)
        else:
            assert False, tosca_type
    assert ctor, f"no ctor for {tosca_type}"
    if tosca_type == "range":
        upper = (
            sys.maxsize if value[1] == "UNBOUNDED" else value[1]
        )  # note: sys.maxsize == size_t
        return ToscaValue(ctor((value[0], upper)), typename)
    elif tosca_type == "list":
        if not schema.entry_schema:
            schema.schema["entry_schema"] = dict(type="any")
        filtered = [
            tosca_to_rust(Property(prop.name, v, schema.entry_schema, prop.custom_def))
            for v in value
            if include_value(v)
        ]
        return ToscaValue(
            ctor(filtered),
            typename,
        )
    elif ctor is SimpleValue.map:
        if not schema.entry_schema:
            schema.schema["entry_schema"] = dict(type="any")
        return ToscaValue(
            ctor(
                {
                    k: tosca_to_rust(
                        Property(prop.name, v, schema.entry_schema, prop.custom_def)
                    )
                    for k, v in value.items()
                    if include_value(v)
                }
            ),
            typename,
        )
    else:
        try:
            return ToscaValue(ctor(value), typename)
        except TypeError:
            logger.error(
                f"couldn't convert to rust value: {tosca_type}, {typename}, {type(value)}, {prop.schema.schema}"
            )
            raise


def prop2field(prop: Property) -> Field:
    return Field(
        prop.name,
        FieldValue.Property(tosca_to_rust(prop)),
    )


def filter2term(
    terms: List[CriteriaTerm], node_filter, cap_name: Optional[str]
) -> bool:
    for condition in NodeTemplate.get_filters(node_filter):
        constraints = []
        for constraint in condition.conditions:
            assert isinstance(constraint, ToscaConstraint), constraint

            # convert to ToscaValue (assumes constraint value is always a simple value)
            if constraint.constraint_key == "in_range":
                ctype = "range"
            else:
                ctype = constraint.property_type
            if ctype in ScalarUnit.SCALAR_UNIT_TYPES:
                cvalue = constraint.constraint_value_msg
            else:
                cvalue = constraint.constraint_value
            prop = Property(
                constraint.property_name,
                cvalue,
                dict(type=ctype),
            )
            value = tosca_to_rust(prop)
            c_ctor = getattr(Constraint, constraint.constraint_key, None)
            if not c_ctor:
                # unsupported constraint type (currently unsupported: pattern, schema)
                # we don't want a false positive, so we need to skip solving for this requirement
                logger.warning(
                    f"solver doesn't support this node_filter constraint: {constraint.constraint_key}"
                )
                # XXX node_filter needs to be evaluated in ToscaSpec.find_matching_node()
                return False
            constraints.append(c_ctor(value))
        terms.append(CriteriaTerm.PropFilter(condition.name, cap_name, constraints))
    return True


def convert(
    nt: NodeTemplate, types: Dict[str, List[str]], topology_template: TopologyTemplate
) -> Node:
    # XXX if nt is in nested topology and replaced, partially convert the outer node instead
    assert nt.type_definition
    entity = Node(nt.name, nt.type_definition.type)
    has_restrictions = False
    # print( entity.name )
    types[nt.type_definition.type] = [p.type for p in nt.type_definition.ancestors()]
    for cap in nt.get_capabilities_objects():
        # if cap.name == "feature":
        #     continue
        types[cap.type_definition.type] = [
            p.type for p in cap.type_definition.ancestors()
        ]
        cap_fields = [
            prop2field(prop)
            for prop in cap.get_properties_objects()
            if include_value(prop.value)
        ]
        entity.fields.append(
            Field(cap.name, FieldValue.Capability(cap.type_definition.type, cap_fields))
        )

    for prop in nt.get_properties_objects():
        if include_value(prop.value):
            entity.fields.append(prop2field(prop))

    type_requirements: Dict[str, Dict[str, Any]] = (
        nt.type_definition.requirement_definitions
    )
    for name, req_dict in nt.all_requirements:
        type_req_dict: Optional[Dict[str, Any]] = type_requirements.get(name)
        on_type_only = not bool(req_dict)
        if type_req_dict:
            req_dict = dict(type_req_dict, **req_dict)
        required = "occurrences" not in req_dict or req_dict["occurrences"][0]
        match_type = not on_type_only or required
        field, found_restrictions = get_req_terms(
            nt, types, topology_template, name, req_dict, match_type
        )
        # print("terms", terms)
        if field:
            entity.fields.append(field)
        if found_restrictions:
            has_restrictions = True
    # print("rels", nt.relationships, nt.missing_requirements)
    entity.has_restrictions = has_restrictions
    return entity


def add_match(terms, match):
    if isinstance(match, dict) and (node_type := match.get("get_nodes_of_type")):
        terms.append(CriteriaTerm.NodeType(node_type))
    else:
        skip = False
        query = []
        result = analyze_expr(match)
        if result:
            expr_list = result.get_keys()
            # logger.warning(f"{match} {expr_list=}")
            query_type = None
            for key in expr_list:
                if key == "$start":
                    continue
                if query_type is not None:
                    query.append((query_type, key))
                    query_type = None
                else:
                    if key.startswith("."):
                        if key == ".configured_by":
                            query.append(
                                (
                                    QueryType.RequiredByType,
                                    "unfurl.relationships.Configures",
                                )
                            )
                        elif key == ".hosted_on":
                            query.append(
                                (
                                    QueryType.TransitiveRelationType,
                                    "tosca.relationships.HostedOn",
                                )
                            )
                        else:
                            query_type = getattr(QueryType, key[1:].title(), None)
                            if query_type is None:
                                skip = True
                                break
                    else:  # key is prop
                        query.append((QueryType.PropSource, key))
                    # logger.warning(f"{skip} {query=}")
        if query and not skip:
            terms.append(CriteriaTerm.NodeMatch(query))


def get_req_terms(
    node_template: NodeTemplate,
    types: Dict[str, List[str]],
    topology_template: TopologyTemplate,
    name: str,
    req_dict: dict,
    match_type: bool,
) -> Tuple[Optional[Field], bool]:
    terms = []
    restrictions = []
    capability = req_dict.get("capability")
    if capability:
        cap_type = topology_template.find_type(
            capability, req_dict.get("!namespace-capability")
        )
        if not cap_type:
            terms.append(CriteriaTerm.CapabilityName(capability))
        elif match_type:
            # only match by type if the template has declared the requirement
            terms.append(CriteriaTerm.CapabilityTypeGroup([capability]))
    rel_type = None
    relationship = req_dict.get("relationship")
    if relationship:
        relname = node_template.get_rel_typename(name, req_dict)
        # print("!relname", name, relationship, relname)
        if relname:
            rel = cast(
                Optional[RelationshipType],
                topology_template.find_type(
                    relname, req_dict.get("!namespace-relationship")
                ),
            )
            # print("!relname2", rel, rel and rel.valid_target_types)
            if rel:
                rel_type = rel.type
                types[rel.type] = [p.type for p in rel.ancestors()]
                if rel.valid_target_types:
                    terms.append(
                        CriteriaTerm.CapabilityTypeGroup(rel.valid_target_types)
                    )
    node = req_dict.get("node")
    if node:
        if node in topology_template.node_templates:
            # XXX if node_template.substitution: node_template.substitution.add_relationship(name, node)  # replacement nested node template with this outer one
            terms.append(CriteriaTerm.NodeName(node))
        elif match_type:
            # only match by type if the template declared the requirement
            terms.append(CriteriaTerm.NodeType(node))
        node_filter = req_dict.get("node_filter")
        if node_filter:
            # print("node_filter", node_filter)
            match = node_filter.get("match")
            if match:
                add_match(terms, match)
            if not filter2term(terms, node_filter, None):
                return None, False
            for cap_filters in node_filter.get("capabilities", []):
                cap_name, cap_filter = list(cap_filters.items())[0]
                if not filter2term(terms, cap_filter, cap_name):
                    return None, False
            for req_req in node_filter.get("requirements") or []:
                req_req_name = list(req_req)[0]
                req_field, _ = get_req_terms(
                    node_template,
                    types,
                    topology_template,
                    req_req_name,
                    req_req[req_req_name],
                    True,
                )
                if req_field:
                    restrictions.append(req_field)
            # XXX add properties that are value constraints
    if terms:
        return Field(name, FieldValue.Requirement(terms, rel_type, restrictions)), bool(
            restrictions
        )
    else:
        return None, False


def solve_topology(topology_template: TopologyTemplate) -> Solution:
    types: Dict[str, List[str]] = {}
    nodes = {}
    for node_template in topology_template.node_templates.values():
        node = convert(node_template, types, topology_template)
        nodes[node.name] = node

    # print("missing", topology_template.node_templates["app"].missing_requirements)
    # print ('types', types)
    logger.debug("!solving " + "\n\n".join(repr(n) for n in nodes.values()))
    solved = cast(Solution, solve(nodes, types))
    logger.debug(f"!solved!")  #  {solved}")
    for (source_name, req), targets in solved.items():
        source = topology_template.node_templates[source_name]
        if len(targets) > 1:
            # filter out defaults
            target_nodes = [
                (t, cap)
                for (t, cap) in (
                    (topology_template.node_templates[node], cap)
                    for (node, cap) in targets
                )
                if "default" not in t.directives
            ]
            # XXX if node filter: report ambiguity
            if not target_nodes:
                continue  # hmm... more than one default match?
            if len(target_nodes) > 1:
                # XXX don't just skip, only treat as error if exceeds occurrences
                continue
            target_node, cap = target_nodes[0]
        else:
            assert targets
            target_node = topology_template.node_templates[targets[0][0]]
            cap = targets[0][1]
        target = target_node.name
        # print("solved", source, req, target_node, cap)
        # pass target to handle case when there is more than one match per requirement
        req_dict = source.find_or_add_requirement(req, target)
        req_dict["node"] = target
        if cap != "feature":
            req_dict["capability"] = cap
    return solved
