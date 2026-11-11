# Types for `unfurl._rust`, the extension module built from `rust/py`, kept by
# hand from `rust/solver`.

import builtins
from typing import Dict, List, Optional, Tuple

from .solver import Node

class QueryType:
    TransitiveRelation: QueryType
    TransitiveRelationType: QueryType
    RequiredBy: QueryType
    RequiredByType: QueryType
    Sources: QueryType
    Targets: QueryType
    PropSource: QueryType
    EntityType: QueryType

Query = List[Tuple[QueryType, str, str]]

class SimpleValue:
    class integer(SimpleValue):
        v: int
        def __init__(self, v: int) -> None: ...

    class string(SimpleValue):
        v: str
        def __init__(self, v: str) -> None: ...

    class boolean(SimpleValue):
        v: bool
        def __init__(self, v: bool) -> None: ...

    class float(SimpleValue):
        v: builtins.float
        def __init__(self, v: builtins.float) -> None: ...

    class list(SimpleValue):
        v: List[ToscaValue]
        def __init__(self, v: List[ToscaValue]) -> None: ...

    class range(SimpleValue):
        v: Tuple[int, int]
        def __init__(self, v: Tuple[int, int]) -> None: ...

    class map(SimpleValue):
        v: Dict[str, ToscaValue]
        def __init__(self, v: Dict[str, ToscaValue]) -> None: ...

class ToscaValue:
    v: SimpleValue
    type_name: Optional[str]
    def __init__(self, value: SimpleValue, name: Optional[str] = None) -> None: ...

class Constraint:
    v: ToscaValue
    class equal(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class greater_than(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class greater_or_equal(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class less_than(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class less_or_equal(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class in_range(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class valid_values(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class length(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class min_length(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class max_length(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class version(Constraint):
        def __init__(self, v: ToscaValue) -> None: ...

    class pattern(Constraint):
        compiled: CompiledPattern
        def __init__(self, v: ToscaValue, compiled: CompiledPattern) -> None: ...

class CompiledPattern:
    @property
    def pattern(self) -> str: ...

class CriteriaTerm:
    class NodeName(CriteriaTerm):
        n: str
        def __init__(self, n: str) -> None: ...

    class NodeType(CriteriaTerm):
        n: str
        def __init__(self, n: str) -> None: ...

    class CapabilityName(CriteriaTerm):
        n: str
        def __init__(self, n: str) -> None: ...

    class CapabilityTypeGroup(CriteriaTerm):
        names: List[str]
        def __init__(self, names: List[str]) -> None: ...

    class PropFilter(CriteriaTerm):
        n: str
        capability: Optional[str]
        constraints: List[Constraint]
        def __init__(
            self, n: str, capability: Optional[str], constraints: List[Constraint]
        ) -> None: ...

    class NodeMatch(CriteriaTerm):
        start_node: str
        query: Query
        def __init__(self, start_node: str, query: Query) -> None: ...

class FieldValue:
    class Property(FieldValue):
        value: Optional[ToscaValue]
        computed: Optional[Tuple[str, Query]]
        def __init__(
            self, value: Optional[ToscaValue], computed: Optional[Tuple[str, Query]]
        ) -> None: ...

    class Capability(FieldValue):
        tosca_type: str
        properties: List[Field]
        def __init__(self, tosca_type: str, properties: List[Field]) -> None: ...

    class Requirement(FieldValue):
        terms: List[CriteriaTerm]
        tosca_type: Optional[str]
        restrictions: List[Field]
        def __init__(
            self,
            terms: List[CriteriaTerm],
            tosca_type: Optional[str],
            restrictions: List[Field],
        ) -> None: ...

class Field:
    name: str
    value: FieldValue
    def __init__(self, name: str, value: FieldValue) -> None: ...
    def has_field_type(self, value: FieldValue) -> bool: ...

def solve(
    nodes: Dict[str, Node], type_parents: Dict[str, List[str]]
) -> Dict[Tuple[str, str], List[Tuple[str, str]]]: ...
def make_pattern_constraint(pattern: str) -> Constraint: ...
def regex_match_exact(pattern: Constraint, text: str) -> bool: ...
