# Copyright (c) 2024 Adam Souzis
# SPDX-License-Identifier: MIT
from typing import Any, Dict, List, Optional, Tuple, Union, cast
import sys

# import types from rust extension
from .tosca_solver import (  # type: ignore
    solve,
    CriteriaTerm,
    Field,
    FieldValue,
    ToscaValue,
    SimpleValue,
    Constraint,
    QueryType,
    make_pattern_constraint,
)
from tosca import EvalData
from tosca import has_function
from toscaparser.elements.relationshiptype import RelationshipType
from toscaparser.elements.scalarunit import get_scalarunit_class, ScalarUnit
from toscaparser.elements.constraints import Schema, Constraint as ToscaConstraint
from toscaparser.activities import value_to_type
from toscaparser.properties import Property
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.capabilities import Capability
from toscaparser.artifacts import Artifact
from toscaparser.topology_template import TopologyTemplate, find_type
from toscaparser.common import exception
from .eval import Ref, analyze_expr
from .logs import getLogger

logger = getLogger("unfurl")

Solution = Dict[Tuple[str, str], List[Tuple[str, str]]]

# the solver treats artifacts just like capabilities, just use a prefix to disambiguate them
_ARTIFACT_PREFIX = "a~"

def intern(s: str) -> str:
    return sys.intern(str(s))


# note: make sure Node in rust/lib.rs staying in sync
class Node:
    """A partial representations of a TOSCA node template (enough for [solve()])"""

    def __init__(self, name, type="tosca.nodes.Root", fields=None):
        self.name: str = name
        self.tosca_type: str = type
        self.fields: List[Field] = fields or []
        # Set if any of its fields has restrictions
        self.has_restrictions: bool = False
        self.required: List[str] = []
        # extra attributes for bookkeeping (not used in rust):
        self._reqs: Dict[str, int] = {}
        # req_name -> target node_name for explicit node targets
        self._assigned_reqs: Dict[str, str] = {}

    def __repr__(self) -> str:
        return f"Node({self.name!r}, {self.tosca_type!r}, {self.has_restrictions}, {self.required!r}, {self.fields!r})"


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
    assert value is not None, (
        f"nulls should be omitted from solver {prop.schema.schema}"
    )
    schema = prop.schema
    entity = prop.entry_schema_entity or prop.entity
    tosca_type = schema.type
    if tosca_type == "string" and not isinstance(value, str):
        tosca_type = "any"
    ctor = getattr(SimpleValue, tosca_type, None)
    try:
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
                tosca_to_rust(
                    Property(prop.name, v, schema.entry_schema, prop.custom_def)
                )
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
            return ToscaValue(ctor(value), typename)
    except TypeError:
        logger.error(
            f"couldn't convert to rust value: {tosca_type}, {typename}, {type(value)}, {prop.schema.schema}"
        )
        raise


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
            if constraint.constraint_key == "pattern":
                from unfurl import support

                if support.pattern_constraint_class:
                    obj = constraint.constraint_value
                    if isinstance(obj, Constraint):
                        constraints.append(obj)
                        continue
                    # otherwise the pattern wasn't a valid rust regex
                c_ctor = None  # rust regex patterns are disabled
            else:
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
        terms.append(
            CriteriaTerm.PropFilter(
                intern(condition.name),
                intern(cap_name) if cap_name else cap_name,
                constraints,
            )
        )
    return True


def _req_is_defined(req_dict):
    if not req_dict:
        return False
    if len(req_dict) == 1 and "relationship" in req_dict:
        # ignore relationship, see section 3.8.2 p140, relationship defines or selects a relationship template, not the node
        return False
    return True


QueryTerms = List[Tuple[QueryType, str, str]]


class TopologyGraph:
    """Converts TOSCA topology templates to solver representation"""

    def __init__(
        self,
        topology_template: TopologyTemplate,
        constrain_required: bool = False,
    ):
        self.topology_template = topology_template
        self.constrain_required = constrain_required
        self.types: Dict[str, List[str]] = {}

    def add_fields(
        self,
        cap: Union[Capability, Artifact],
        node_template: NodeTemplate,
        entity: Node,
        prefix: str,
    ):
        """Add capability or artifact fields to a node entity"""
        assert cap.type_definition
        self.types[intern(cap.type_definition.global_name)] = [
            intern(p.global_name) for p in cap.type_definition.ancestors()
        ]
        cap_fields: List[Field] = list(
            filter(
                None,
                [
                    self.prop2field(node_template, cap, prop, prefix)
                    for prop in cap.get_properties_objects()
                ],
            )
        )
        for prop in cap.builtin_properties().values():
            field = self.prop2field(node_template, cap, prop, prefix)
            if field:
                cap_fields.append(field)

        entity.fields.append(
            Field(
                intern(prefix + cap.name),
                FieldValue.Capability(
                    intern(cap.type_definition.global_name), cap_fields
                ),
            )
        )

    def _create_node(self, node_template, solved) -> Node:
        # XXX if node_template is in nested topology and replaced, partially convert the outer node instead
        node_name = intern(node_template.name)
        if not node_template.type_definition:
            return Node(node_name, intern("tosca.nodes.Root"))
        entity = Node(
            node_name,
            intern(node_template.type_definition.global_name),
        )
        self.types[intern(node_template.type_definition.global_name)] = [
            intern(p.global_name) for p in node_template.type_definition.ancestors()
        ]
        return entity

    # XXX when adding solved topologies
    # def _add_found_only(self, node_template: NodeTemplate):
    #     # entity.fields.append(field)
    #     for r in node_template.requirements:
    #         name, value = next(iter(r.items()))  # list only has one item
    #         if isinstance(value, dict):
    #             node = value.get("node")
    #             if node in node_template.topology:
    #                 node = value
    #         else:
    #             node = value

    def convert(self, node_template: NodeTemplate, solved_only=False) -> Node:
        """Convert a NodeTemplate to a solver Node representation"""
        entity = self._create_node(node_template, solved_only)
        if not node_template.type_definition:
            return entity
        has_restrictions = False
        for cap in node_template.get_capabilities_objects():
            # if cap.name == "feature":
            #     continue
            self.add_fields(cap, node_template, entity, "")
        for artifact in node_template.artifacts.values():
            self.add_fields(artifact, node_template, entity, _ARTIFACT_PREFIX)
        for prop in node_template.get_properties_objects():
            field = self.prop2field(node_template, None, prop)
            if field:
                entity.fields.append(field)
        for prop in node_template.builtin_properties().values():
            field = self.prop2field(node_template, None, prop)
            if field:
                entity.fields.append(field)

        type_requirements: Dict[str, Dict[str, Any]] = (
            node_template.type_definition.requirement_definitions
        )
        for name, req_dict in node_template.all_requirements:
            type_req_dict: Optional[Dict[str, Any]] = type_requirements.get(name)
            # req_dict will be empty if only defined on the type
            on_template = _req_is_defined(req_dict)
            if type_req_dict:
                type_req_dict = type_req_dict.copy()
                for key in ("node", "relationship", "capability"):
                    if (
                        key in req_dict
                    ):  # overridden on template, so remove namespace-id
                        type_req_dict.pop("!namespace-" + key, None)
                req_dict = dict(type_req_dict, **req_dict)
            if "occurrences" not in req_dict:
                required = True
                upper = sys.maxsize
            else:
                required = bool(req_dict["occurrences"][0])
                max_occurrences = req_dict["occurrences"][1]
                upper = (
                    sys.maxsize
                    if max_occurrences == "UNBOUNDED"
                    else int(max_occurrences)
                )
            # note: ok if multiple requirements with same name on the template, then occurrences should be on type
            entity._reqs[name] = upper
            match_type = (
                req_dict.get("node_filter")
                or on_template
                or (self.constrain_required and required)
            )
            field, found_restrictions = self.get_req_terms(
                node_template, name, req_dict, match_type
            )
            if field:
                entity.fields.append(field)
                # track explicit node targets for checking conditional deps
                node_target = req_dict.get("node")
                if node_target and node_target in self.topology_template.node_templates:
                    entity._assigned_reqs[name] = self._resolve_node_name(node_target)
            if found_restrictions:
                has_restrictions = True
            if required and "conditional" in node_template.directives:
                entity.required.append(intern(name))
        # print("rels", node_template.relationships, node_template.missing_requirements)
        entity.has_restrictions = has_restrictions
        return entity

    def prop2field(
        self,
        node_template: NodeTemplate,
        cap: Union[Capability, Artifact, None],
        prop: Property,
        prefix: str = "",
    ) -> Optional[Field]:
        """Convert a TOSCA property to a solver Field"""
        query = None
        if prop.value and has_function(prop.value):
            if not Ref.is_ref(prop.value):
                return None
            if cap:
                # adjust query add a capability to start if relative
                # hacky: query works for artifacts too because of prefix
                querystart = (
                    f"{node_template.name}::.capabilities::[.name={prefix}{cap.name}]"
                )
                expr = EvalData(prop.default).set_start(querystart).as_expr
            else:
                expr = prop.default
            start_node, terms = self.expr2query(node_template, expr)
            if terms:
                query = (start_node, terms)
            else:
                return None
            value = None
        elif prop.value is not None:
            value = tosca_to_rust(prop)
        else:
            return None
        return Field(intern(prop.name), FieldValue.Property(value, query))

    def add_match(
        self,
        node_template: NodeTemplate,
        terms: list,
        match: Union[str, dict],
        namespace: Optional[str],
    ) -> None:
        """Add a match criteria to the terms list"""
        if isinstance(match, dict) and (
            node_type_name := match.get("get_nodes_of_type")
        ):
            node_type = node_template.topology_template.find_type(
                node_type_name, namespace
            )
            terms.append(
                CriteriaTerm.NodeType(
                    node_type.global_name if node_type else node_type_name
                )
            )
        else:
            start_node, query = self.expr2query(node_template, match)
            if query:
                terms.append(CriteriaTerm.NodeMatch(start_node, query))

    def _resolve_node_name(self, node: str) -> str:
        # XXX if this node template was replaced with outer one
        # we need to add outer topology to topology and return qualified outer topology name
        return node

    def expr2query(
        self, node_template: NodeTemplate, match: Union[str, dict, list, None]
    ) -> Tuple[str, QueryTerms]:
        """Convert an expression to a query representation"""
        from .support import resolve_function_keyword  # avoid circular import

        if isinstance(match, dict) and (get_property := match.get("get_property")):
            assert isinstance(get_property, list), get_property
            match = cast(str, resolve_function_keyword(get_property[0]))
            if len(get_property) > 2:
                match += "::.capabilities"  # XXX or .targets (cf. .names)
            match += "::" + "::".join(get_property[1:])

        query: QueryTerms = []
        start_node: str = node_template.name
        result = analyze_expr(match, ["SOURCE"])
        if result:
            expr_list = result.get_keys()
            # logger.trace(f"expr_list: {node_template.name} with {match}:\n {expr_list=}")
            query_type = None
            cap = ""
            for key in expr_list:
                if key == "$start":
                    continue
                if key == "SOURCE":
                    start_node = node_template.name
                    continue
                if cap == ".capabilities":
                    cap = key  # assume this key is capability name
                    continue
                if cap == ".artifacts":
                    cap = _ARTIFACT_PREFIX + key  # assume this key is artifact name
                    continue
                if query_type is not None:
                    # Sources or Targets consume next key
                    if query_type == QueryType.EntityType:
                        typedef = find_type(key, node_template.custom_def)
                        if typedef:
                            key = typedef.global_name
                    query.append((query_type, intern(key), ""))
                    query_type = None
                else:
                    if key.startswith("::"):
                        query = []
                        start_node = self._resolve_node_name(key[2:])
                    elif key.startswith("."):
                        if key == ".":
                            continue
                        elif key == "..":
                            query.append((QueryType.Targets, "host", ""))
                        elif key.startswith(".root"):
                            query = []
                            start_node = "root"
                        elif key == ".instances":
                            query.append((QueryType.Sources, "host", ""))
                        elif key == ".configured_by":
                            query.append(
                                (
                                    QueryType.RequiredByType,
                                    intern("unfurl.relationships.Configures"),
                                    "",
                                )
                            )
                        elif key == ".parents" or key == ".ancestors":
                            query.append(
                                (
                                    QueryType.TransitiveRelation,
                                    "host",
                                    "SELF" if key == ".ancestors" else "",
                                )
                            )
                        elif key == ".hosted_on":
                            query.append(
                                (
                                    QueryType.TransitiveRelationType,
                                    intern("tosca.relationships.HostedOn"),
                                    "",
                                )
                            )
                        elif key == ".capabilities":
                            cap = ".capabilities"  # assume next key is capability name
                        elif key == ".artifacts":
                            cap = ".artifacts"  # assume next key is artifact name
                        elif key == ".type":
                            query_type = QueryType.EntityType
                        else:
                            # matches Sources or Targets
                            query_type = getattr(QueryType, key[1:].title(), None)
                            if query_type is None:
                                return "", []
                    else:  # key is prop
                        query.append((QueryType.PropSource, intern(key), intern(cap)))
                        cap = ""
        # logger.trace(f"{start_node} with {match}:\n {query=}")
        return start_node, query

    def get_req_terms(
        self,
        node_template: NodeTemplate,
        name: str,
        req_dict: dict,
        match_type: bool,
    ) -> Tuple[Optional[Field], bool]:
        """Get requirement terms for solver matching"""
        terms = []
        restrictions = []
        capability = req_dict.get("capability")
        if capability:
            cap_type = self.topology_template.find_type(
                capability, req_dict.get("!namespace-capability")
            )
            if not cap_type:
                terms.append(CriteriaTerm.CapabilityName(intern(capability)))
            elif match_type:
                # only match by type if the template has declared the requirement
                terms.append(
                    CriteriaTerm.CapabilityTypeGroup([intern(cap_type.global_name)])
                )
        rel_type = None
        relationship = req_dict.get("relationship")
        if relationship and match_type:
            relname = node_template.get_rel_typename(name, req_dict)
            if relname:
                rel = cast(
                    Optional[RelationshipType],
                    self.topology_template.find_type(
                        relname, req_dict.get("!namespace-relationship")
                    ),
                )
                if rel:
                    rel_type = rel.global_name
                    # special case "host":
                    if name == "host" and rel_type == "tosca.relationships.Root":
                        rel_type = "tosca.relationships.HostedOn"
                        rel = cast(
                            RelationshipType,
                            self.topology_template.find_type(
                                "tosca.relationships.HostedOn"
                            ),
                        )
                    self.types[rel.global_name] = [
                        p.global_name for p in rel.ancestors()
                    ]
                    if rel.valid_target_types:
                        valid_target_types = []
                        for target_type_name in rel.valid_target_types:
                            target_type = find_type(target_type_name, rel.custom_def)
                            valid_target_types.append(
                                target_type.global_name
                                if target_type
                                else target_type_name
                            )
                        terms.append(
                            CriteriaTerm.CapabilityTypeGroup(
                                [t for t in valid_target_types]
                            )
                        )
        node = req_dict.get("node")
        node_namespace = req_dict.get("!namespace-node")
        if node:
            node_name = ""
            # # XXX node might be in a different topology, if so, add that topology
            # if node_namespace:
            #     target_node = node_template.find_node_related_template(
            #         node, node_namespace
            #     )
            #     if target_node:
            #         node_name = target_node.name
            # else:
            if node in self.topology_template.node_templates:
                node_name = self._resolve_node_name(node)
            if node_name:
                terms.append(CriteriaTerm.NodeName(node_name))
            elif match_type:
                node_type = self.topology_template.find_type(node, node_namespace)
                if node_type:
                    # only match by type if the template declared the requirement
                    terms.append(CriteriaTerm.NodeType(intern(node_type.global_name)))
                else:  # add missing node so we don't match
                    terms.append(CriteriaTerm.NodeName(node))
        node_filter = req_dict.get("node_filter")
        if node_filter:
            matches = node_filter.get("match")
            if matches:
                for match in matches:
                    self.add_match(node_template, terms, match, node_namespace)
            if not filter2term(terms, node_filter, None):
                return None, False  # has an unsupported constraint, bail
            for key in ["capabilities", "artifacts"]:
                for cap_filters in node_filter.get(key, []):
                    cap_name, cap_filter = list(cap_filters.items())[0]
                    cap_type = self.topology_template.find_type(cap_name)
                    if cap_type:
                        cap_name = cap_type.global_name
                        if cap_name not in self.types:
                            self.types[cap_name] = [
                                p.global_name for p in cap_type.ancestors()
                            ]
                        terms.append(
                            CriteriaTerm.CapabilityTypeGroup([intern(cap_name)])
                        )
                    elif key == "artifacts":
                        cap_name = _ARTIFACT_PREFIX + cap_name
                    if not filter2term(terms, cap_filter, cap_name):
                        return None, False  # has an unsupported constraint, bail
            for req_req in node_filter.get("requirements") or []:
                req_req_name = list(req_req)[0]
                req_field, _ = self.get_req_terms(
                    node_template,
                    req_req_name,
                    req_req[req_req_name],
                    match_type,  # XXX
                )
                if req_field:
                    restrictions.append(req_field)
            for condition in node_filter.get("properties") or []:
                assert isinstance(condition, dict)
                key, value = list(condition.items())[0]
                if isinstance(value, dict):
                    if "eval" in value:
                        start_node, query = self.expr2query(node_template, value)
                        if query:
                            restrictions.append(
                                Field(
                                    key,
                                    FieldValue.Property(None, (start_node, query)),
                                )
                            )
                    elif "q" in value:
                        val = value["q"]
                        if val is not None:
                            tosca_type = value_to_type(val) or "any"
                            prop = Property(key, val, dict(type=tosca_type))
                            restrictions.append(
                                Field(
                                    key,
                                    FieldValue.Property(tosca_to_rust(prop), None),
                                )
                            )
        if terms or restrictions:
            return Field(
                name, FieldValue.Requirement(terms, rel_type, restrictions)
            ), bool(restrictions)
        else:
            return None, False


def solve_topology(
    topology_template: TopologyTemplate, resolve_select=None, constrain_required=False
) -> Solution:
    if not topology_template.node_templates:
        return {}
    graph = TopologyGraph(topology_template, constrain_required)
    nodes: Dict[str, Node] = {}
    for node_template in topology_template.node_templates.values():
        if resolve_select and "select" in node_template.directives:
            node_template = resolve_select(node_template)
            if not node_template:
                continue
        node = graph.convert(node_template)
        nodes[node.name] = node
        # print("missing", node_template.missing_requirements)
    # print ('types', graph.types)
    # print("!solving " + "\n\n".join(repr(n) for n in nodes.values()))
    # graph.add_referenced_topologies()
    solved = cast(Solution, solve(nodes, graph.types))
    logger.debug(
        f"Solver found {len(solved)} requirements match{'es' if len(solved) != 1 else ''} for {len(nodes)} Node template{'s' if len(nodes) != 1 else ''}."
    )
    # print("SOLVED", solved)
    for (source_name, req), targets in solved.items():
        # XXX if in_referenced_topology(source_name): continue
        source: NodeTemplate = topology_template.node_templates[source_name]
        target_nodes = [
            (topology_template.node_templates[node], cap) for (node, cap) in targets
        ]
        # XXX if in_referenced_topology(target_node): use substitution mapping if found or add "!namespace-node" to req_dict
        if len(targets) > 1:
            # filter out default nodes
            no_defaults = [
                (t, cap) for (t, cap) in target_nodes if "default" not in t.directives
            ]
            if no_defaults:
                target_nodes = no_defaults
            max_occurrences = nodes[source_name]._reqs[req]
            if len(target_nodes) > max_occurrences:
                exception.ExceptionCollector.appendException(
                    exception.ValidationError(
                        message='requirement "%s" of node "%s" found %s targets more than max occurrences %s'
                        % (req, source_name, len(target_nodes), max_occurrences)
                    )
                )
        for target_node, cap in target_nodes:
            _set_target(source, req, cap, target_node)
    # mark requirements with an explicit target that wasn't found by the solver as invalid
    for node in nodes.values():
        if node._assigned_reqs:
            for req_name, target_name in node._assigned_reqs.items():
                # solver skips nodes that reference themselves, so ignore those
                if target_name != node.name and (node.name, req_name) not in solved:
                    node_template = topology_template.node_templates[node.name]
                    node_template._invalid_requirements[req_name] = target_name
    return solved


def _set_target(
    source: NodeTemplate, req_name: str, cap: str, target_node: NodeTemplate
) -> None:
    # updates requirements yaml directly so NodeTemplate won't search for a match later
    target = target_node.name
    req_dict: dict = source.find_or_add_requirement(req_name, target)
    changed = ""
    # XXX if nested, copy tpl if modifying
    # XXX if target_node is in different topology need to add target to _outer_relationships
    if req_dict.get("node") != target:
        index = req_dict.get("minimized")
        if index is not None:
            cast(list, source.requirements)[index][req_name] = target
        else:
            req_dict["node"] = target
        changed = "node"
    if (
        cap != "feature"
        and not cap.startswith(_ARTIFACT_PREFIX)
        and req_dict.get("capability") != cap
    ):
        req_dict["capability"] = cap
        changed = "cap"
    if changed == "node":
        logger.trace(f"Solver set {source.name}.{req_name} to {target}")
    elif changed == "cap":
        logger.trace(
            f"Solver set {source.name}.{req_name} to {target} with capability {cap}"
        )
