// Copyright (c) 2024 Adam Souzis
// SPDX-License-Identifier: MIT
#![allow(clippy::let_unit_value)] // ignore for ascent!
#![allow(clippy::collapsible_if)] // ignore for ascent!
#![allow(clippy::clone_on_copy)] // ignore for ascent!
#![allow(clippy::unused_enumerate_index)] // ignore for ascent!
#![allow(clippy::type_complexity)] // ignore for ascent!

use ascent::{ascent, lattice::set::Set};
use std::convert::{From, Into};
use std::{cmp::Ordering, collections::BTreeMap, fmt::Debug, hash::Hash};

#[cfg(feature = "python")]
use pyo3::prelude::*;

pub type Symbol = String;

type EntityName = Symbol;
type NodeName = EntityName;
type AnonEntityId = EntityName;
type CapabilityName = Symbol;
type PropName = Symbol;
type ReqName = Symbol;
pub type TypeName = Symbol;
type QueryId = usize;
type Query = Vec<(QueryType, Symbol, Symbol)>;

#[inline]
pub(crate) fn sym(s: &str) -> Symbol {
    // XXX make Symbol a real symbol, e.g. maybe use https://github.com/CAD97/strena/blob/main/src/lib.rs#L329C12-L329C25
    s.to_string()
}

/// Represents the match criteria for a requirement.
///
/// Corresponds to "node", "capability", and "node_filter"
/// fields on a TOSCA requirement and "valid_target_types" on relationship types.
#[cfg_attr(feature = "python", pyclass)]
#[derive(Clone, PartialOrd, Ord, PartialEq, Eq, Hash, Debug)]

pub enum CriteriaTerm {
    NodeName {
        n: Symbol,
    },
    NodeType {
        n: Symbol,
    },
    CapabilityName {
        n: Symbol,
    },
    CapabilityTypeGroup {
        names: Vec<Symbol>,
    },
    PropFilter {
        n: Symbol,
        capability: Option<Symbol>,
        constraints: Vec<Constraint>,
    },
    NodeMatch {
        start_node: Symbol,
        query: Query,
    },
}

impl CriteriaTerm {
    #[allow(unused)]
    fn variant_id(&self) -> usize {
        match self {
            CriteriaTerm::NodeName { .. } => 1,
            CriteriaTerm::NodeType { .. } => 2,
            CriteriaTerm::CapabilityName { .. } => 3,
            CriteriaTerm::CapabilityTypeGroup { .. } => 4,
            CriteriaTerm::PropFilter { .. } => 5,
            CriteriaTerm::NodeMatch { .. } => 6,
        }
    }

    fn match_property(&self, t: &ToscaValue) -> bool {
        match self {
            CriteriaTerm::PropFilter { constraints, .. } => {
                !constraints.is_empty()
                    && constraints.iter().all(|i| i.matches(t).is_some_and(|s| s))
            }
            _ => false, // always false if we're not a CriteriaTerm::PropFilter
                        // CriteriaTerm::NodeName { n } => match (t.v) { TValue::string { v,} => v == *n, _ => false },
                        // CriteriaTerm::NodeType { n } => match (t.v) { TValue::string { v,} => v == *n, _ => false },
                        // CriteriaTerm::CapabilityName { n } => match (t.v) { TValue::string { v,} => v == *n, _ => false },
        }
    }
}

#[cfg_attr(feature = "python", pyclass(eq, eq_int))]
#[derive(Copy, Clone, PartialOrd, Ord, PartialEq, Eq, Hash, Debug)]
pub enum QueryType {
    TransitiveRelation,
    TransitiveRelationType,
    RequiredBy,
    RequiredByType,
    Sources,
    Targets,
    PropSource,
}

/// Constraints used in node filters
#[allow(non_camel_case_types)]
#[cfg_attr(feature = "python", pyclass)]
#[derive(Clone, PartialEq, Eq, Hash, Debug)]
pub enum Constraint {
    equal { v: ToscaValue },
    greater_than { v: ToscaValue },
    greater_or_equal { v: ToscaValue },
    less_than { v: ToscaValue },
    less_or_equal { v: ToscaValue },
    in_range { v: ToscaValue },
    valid_values { v: ToscaValue },
    length { v: ToscaValue },
    min_length { v: ToscaValue },
    max_length { v: ToscaValue },
    // pattern, // XXX
    // schema,  // XXX
}

impl Constraint {
    fn get_value(&self) -> &ToscaValue {
        match self {
            Constraint::equal { v } => v,
            Constraint::greater_than { v } => v,
            Constraint::greater_or_equal { v } => v,
            Constraint::less_than { v } => v,
            Constraint::less_or_equal { v } => v,
            Constraint::in_range { v } => v,
            Constraint::valid_values { v } => v,
            Constraint::length { v } => v,
            Constraint::min_length { v } => v,
            Constraint::max_length { v } => v,
        }
    }

    fn matches(&self, t: &ToscaValue) -> Option<bool> {
        // XXX validate self.v is compatibility with v
        // let v = self.get_value();
        // let t = tc.v;
        match self {
            Constraint::equal { v } => Some(t == v),
            Constraint::greater_than { v } => Some(t > v),
            Constraint::greater_or_equal { v } => Some(t >= v),
            Constraint::less_than { v } => Some(t < v),
            Constraint::less_or_equal { v } => Some(t <= v),
            Constraint::in_range {
                v:
                    ToscaValue {
                        v: SimpleValue::range { v: sv },
                        ..
                    },
            } => Some(
                t.v >= SimpleValue::integer { v: sv.0 } && t.v <= SimpleValue::integer { v: sv.1 },
            ),
            Constraint::valid_values {
                v:
                    ToscaValue {
                        v: SimpleValue::list { v: sv },
                        ..
                    },
            } => {
                let found = sv.iter().position(|x| *x == *t);
                Some(found.is_some())
            }
            Constraint::length {
                v:
                    ToscaValue {
                        v: SimpleValue::integer { v: vv },
                        ..
                    },
            } => {
                let len = t.v.len()?;
                Some(*vv == len as i128)
            }
            Constraint::min_length {
                v:
                    ToscaValue {
                        v: SimpleValue::integer { v: vv },
                        ..
                    },
            } => {
                let len = t.v.len()?;
                Some(*vv >= len as i128)
            }
            Constraint::max_length {
                v:
                    ToscaValue {
                        v: SimpleValue::integer { v: vv },
                        ..
                    },
            } => {
                let len = t.v.len()?;
                Some(*vv <= len as i128)
            }
            _ => None, // type mismatch
        }
    }
}

impl PartialOrd for Constraint {
    #[inline]
    fn partial_cmp(&self, other: &Constraint) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

// we need Ord for the lattice
impl Ord for Constraint {
    fn cmp(&self, other: &Constraint) -> Ordering {
        let v = self.get_value();
        let ov = other.get_value();
        match v.partial_cmp(ov) {
            Some(cmp) => cmp,
            // different types of SimpleValues don't compare, so do it here
            // note: this implies NaN == NaN if SimpleValue is a float, which is fine for our usage.
            None => Ord::cmp(&v.v.variant_id(), &ov.v.variant_id()),
        }
    }
}

/// Set of CriteriaTerms
pub type Criteria = Set<CriteriaTerm>;
pub type Restrictions = Vec<Field>;

#[inline]
fn match_criteria(full: &Criteria, current: &Criteria) -> bool {
    full == current
}

/// Simple TOSCA value
#[allow(non_camel_case_types)]
#[cfg_attr(feature = "python", pyclass)]
#[derive(Clone, PartialEq, Debug)]
pub enum SimpleValue {
    // tosca simple values
    integer { v: i128 },
    string { v: String },
    boolean { v: bool },
    float { v: f64 },
    list { v: Vec<ToscaValue> },
    range { v: (i128, i128) },
    map { v: BTreeMap<String, ToscaValue> },
    // XXX "timestamp",
}

impl SimpleValue {
    fn variant_id(&self) -> usize {
        match self {
            SimpleValue::integer { .. } => 1,
            SimpleValue::string { .. } => 2,
            SimpleValue::boolean { .. } => 3,
            SimpleValue::float { .. } => 4,
            SimpleValue::list { .. } => 5,
            SimpleValue::range { .. } => 6,
            SimpleValue::map { .. } => 7,
        }
    }

    fn len(&self) -> Option<usize> {
        match self {
            SimpleValue::string { v } => Some(v.len()),
            SimpleValue::list { v } => Some(v.len()),
            SimpleValue::map { v } => Some(v.len()),
            _ => None,
        }
    }
}

impl PartialOrd for SimpleValue {
    fn partial_cmp(&self, other: &SimpleValue) -> Option<Ordering> {
        match (self, other) {
            (SimpleValue::integer { v }, SimpleValue::integer { v: v2 }) => v.partial_cmp(v2),
            (SimpleValue::string { v }, SimpleValue::string { v: v2 }) => v.partial_cmp(v2),
            (SimpleValue::boolean { v }, SimpleValue::boolean { v: v2 }) => v.partial_cmp(v2),
            (SimpleValue::float { v }, SimpleValue::float { v: v2 }) => v.partial_cmp(v2),
            (SimpleValue::list { v }, SimpleValue::list { v: v2 }) => v.partial_cmp(v2),
            (SimpleValue::range { v }, SimpleValue::range { v: v2 }) => v.partial_cmp(v2),
            (SimpleValue::map { v }, SimpleValue::map { v: v2 }) => v.partial_cmp(v2),
            _ => None, // different types of SimpleValues are not comparable
        }
    }
}

impl Eq for SimpleValue {
    fn assert_receiver_is_total_eq(&self) {
        // skip this check so we can pretend f64 are Eq
        // XXX fix this (use float_eq::FloatEq? or ordered-float
    }
}

impl Hash for SimpleValue {
    #[inline]
    fn hash<__H: ::core::hash::Hasher>(&self, state: &mut __H) {
        let __self_tag = std::mem::discriminant(self);
        Hash::hash(&__self_tag, state);
        match self {
            SimpleValue::integer { v: __self_0 } => Hash::hash(__self_0, state),
            SimpleValue::string { v: __self_0 } => Hash::hash(__self_0, state),
            SimpleValue::boolean { v: __self_0 } => Hash::hash(__self_0, state),
            SimpleValue::float { v: __self_0 } => Hash::hash(&__self_0.to_bits(), state),
            SimpleValue::list { v: __self_0 } => Hash::hash(__self_0, state),
            SimpleValue::range { v: __self_0 } => Hash::hash(__self_0, state),
            SimpleValue::map { v: __self_0 } => Hash::hash(__self_0, state),
        }
    }
}

macro_rules! sv_from {
    ($type:ty, $variant:ident) => {
        impl From<$type> for SimpleValue {
            fn from(item: $type) -> Self {
                SimpleValue::$variant { v: item }
            }
        }
    };
}

sv_from!(i128, integer);
sv_from!(f64, float);
sv_from!(bool, boolean);
sv_from!(String, string);
sv_from!((i128, i128), range);
sv_from!(Vec<ToscaValue>, list);
sv_from!(BTreeMap<String, ToscaValue>, map);

/// A TOSCA value. If a complex value or typed scalar, type_name will be set.
#[cfg_attr(feature = "python", pyclass)]
#[derive(Clone, PartialOrd, PartialEq, Eq, Hash, Debug)]
pub struct ToscaValue {
    #[cfg(feature = "python")]
    #[pyo3(get, set)]
    pub type_name: Option<Symbol>,

    #[cfg(not(feature = "python"))]
    pub type_name: Option<Symbol>,

    #[cfg(feature = "python")]
    #[pyo3(get)]
    pub v: SimpleValue,

    #[cfg(not(feature = "python"))]
    pub v: SimpleValue,
}

#[cfg(feature = "python")]
#[pymethods]
impl ToscaValue {
    #[new]
    #[pyo3(signature = (value, name=None))]
    fn new(value: SimpleValue, name: Option<String>) -> Self {
        ToscaValue {
            type_name: name.map(|n| sym(&n)),
            v: value,
        }
    }

    #[setter]
    fn set_v(&mut self, value: SimpleValue) -> PyResult<()> {
        self.v = value;
        Ok(())
    }
}

macro_rules! tv_from {
    ($type:ty) => {
        impl From<$type> for ToscaValue {
            fn from(item: $type) -> Self {
                ToscaValue {
                    type_name: None,
                    v: SimpleValue::from(item),
                }
            }
        }
    };
}

tv_from!(i128);
tv_from!(f64);
tv_from!(bool);
tv_from!(String);
tv_from!((i128, i128));
tv_from!(Vec<ToscaValue>);
tv_from!(BTreeMap<String, ToscaValue>);

/// Value of a [Node](crate::Node) field.
#[cfg_attr(feature = "python", pyclass)]
#[derive(Clone, PartialOrd, PartialEq, Eq, Hash, Debug)]
pub enum FieldValue {
    Property {
        value: ToscaValue,
        // computed: Option<Query>
    },
    Capability {
        tosca_type: String, // the capability type
        properties: Vec<Field>,
    },
    Requirement {
        terms: Vec<CriteriaTerm>,
        tosca_type: Option<String>, // the relationship type
        restrictions: Vec<Field>, // node_filter requirement or property constraints to apply to the match
    },
}

/// [Node](crate::Node) field.
#[cfg_attr(feature = "python", pyclass)]
#[derive(Clone, PartialOrd, PartialEq, Eq, Hash, Debug)]
pub struct Field {
    #[cfg(feature = "python")]
    #[pyo3(get, set)]
    pub name: String,
    #[cfg(not(feature = "python"))]
    pub name: String,

    #[cfg(feature = "python")]
    #[pyo3(get)]
    pub value: FieldValue,
    #[cfg(not(feature = "python"))]
    pub value: FieldValue,
}

#[cfg(feature = "python")]
#[pymethods]
impl Field {
    #[new]
    fn new(name: String, value: FieldValue) -> Self {
        Field { name, value }
    }

    #[setter]
    fn set_value(&mut self, value: FieldValue) -> PyResult<()> {
        self.value = value;
        Ok(())
    }

    fn __repr__(&self) -> String {
        format!("{self:?}")
    }
}

#[derive(Clone, PartialEq, Eq, Hash)]
pub enum EntityRef {
    Node(NodeName),
    Capability(NodeName, CapabilityName),
    Datatype(AnonEntityId),
    Relationship(NodeName, ReqName),
    Property(NodeName, CapabilityName, PropName),
}

impl EntityRef {
    pub fn is_relationship(&self, node_name: &NodeName, req_name: &ReqName) -> bool {
        matches!(self, Self::Relationship(n, r) if *n == *node_name && *r == *req_name)
    }

    pub fn is_capability(&self, node_name: &NodeName, cap_name: &CapabilityName) -> bool {
        matches!(self, Self::Capability(n, cap) if *n == *node_name && *cap == *cap_name)
    }

    pub fn node_name(&self) -> Option<NodeName> {
        match self {
            Self::Node(n) => Some(n.clone()),
            Self::Capability(n, _) => Some(n.clone()),
            Self::Relationship(n, _) => Some(n.clone()),
            Self::Property(n, ..) => Some(n.clone()),
            _ => None,
        }
    }
    pub fn req_name(&self) -> Option<ReqName> {
        match self {
            Self::Relationship(_, r) => Some(r.clone()),
            _ => None,
        }
    }
}

fn choose_cap(a: Option<CapabilityName>, b: Option<CapabilityName>) -> Option<CapabilityName> {
    match (a, b) {
        (Some(x), Some(y)) => {
            if x == "feature" {
                Some(y)
            } else {
                Some(x)
            }
        }
        (Some(x), None) => Some(x),
        (None, Some(y)) => Some(y),
        _ => None,
    }
}

ascent! {
    #![generate_run_timeout]
    pub(crate) struct Topology;

    relation entity(EntityRef, TypeName);
    relation node(NodeName, TypeName);

    // reqname is set if property is on a relationship template
    relation property_value (NodeName, CapabilityName, ReqName, PropName, ToscaValue);
    // if property is referenced in a node_filter match:
    // translate computed property's eval expression into a query
    relation property_expr (NodeName, CapabilityName, ReqName, PropName, EntityRef);
    // otherwise if property is not computed, add property_source(current, cap, prop_name, current)
    relation property_source (NodeName, CapabilityName, PropName, NodeName);

    // node_template definition
    relation capability (NodeName, CapabilityName, EntityRef);
    relation requirement(NodeName, ReqName, Criteria, Restrictions);
    relation relationship(NodeName, ReqName, TypeName);
    relation req_term_node_name(NodeName, ReqName, CriteriaTerm, NodeName);
    relation req_term_node_type(NodeName, ReqName, CriteriaTerm, TypeName);
    relation req_term_cap_type(NodeName, ReqName, CriteriaTerm, TypeName);
    relation req_term_cap_name(NodeName, ReqName, CriteriaTerm, CapabilityName);
    relation req_term_prop_filter(NodeName, ReqName, CriteriaTerm, CapabilityName, PropName);
    relation req_term_query(NodeName, ReqName, CriteriaTerm, QueryId);
    relation term_match(NodeName, ReqName, Criteria, CriteriaTerm, NodeName, Option<CapabilityName>);
    lattice filtered(NodeName, ReqName, NodeName, Option<CapabilityName>, Criteria, Criteria);
    relation requirement_match(NodeName, ReqName, NodeName, CapabilityName);

    term_match(source, req, criteria, ct, target, None) <--
        node(target, typename), requirement(source, req, criteria, restrictions),
        req_term_node_name(source, req, ct, target) if source != target;

    term_match(source, req, criteria, ct, target, None) <--
        node(target, typename), requirement(source, req, criteria, restrictions),
        req_term_node_type(source, req, ct, typename) if source != target;

    term_match(source, req, criteria, ct, target, Some(cap_name.clone())) <--
        capability(target, cap_name, cap_id), entity(cap_id, typename),
        requirement(source, req, criteria, restrictions),
        req_term_cap_type(source, req, ct, typename) if source != target;

    term_match(source, req, criteria, ct, target, Some(cap_name.clone())) <--
        capability(target, cap_name, _), requirement(source, req, criteria, restrictions),
        term_match(source, req, criteria, _, target, _),  // only match req_term_capname after we found candidate target nodes
        req_term_cap_name(source, req, ct, cap_name);

    term_match(source, req, criteria, ct, target, None) <--
        property_value(target, capname, sym(""), propname, value),
        requirement(source, req, criteria, _),
        req_term_prop_filter(source, req, ct, capname, propname) if source != target && ct.match_property(value);

    term_match(source, req, criteria, ct, target, None) <--
        result(entity_ref, q_id, target, true),
        req_term_query(source, req, ct, q_id) if entity_ref.is_relationship(source, req),
        requirement(source, req, criteria, _);

    filtered(name, req_name, target, cn, criteria, Criteria::singleton(term.clone())) <--
        term_match(name, req_name, criteria, term, target, cn);

    filtered(name, req_name, target, choose_cap(tcn.clone(), fcn.clone()), criteria,
            Set({let mut fc = f.0.clone(); fc.insert(term.clone()); fc})) <--
        term_match(name, req_name, criteria, term, target, tcn),
        filtered(name, req_name, target, fcn, criteria, ?f);

    // if all the criteria have been found, create a requirement_match
    requirement_match(name, req_name, target, fcn.clone().unwrap_or("feature".into())) <--
        filtered(name, req_name, target, fcn, criteria, filter) if match_criteria(filter, criteria);

    // graph navigation
    relation required_by(NodeName, ReqName, NodeName);
    relation transitive_match(NodeName, ReqName, NodeName);

    required_by(y, r, x) <-- requirement_match(x, r, y, c);
    required_by(x, r, z) <-- requirement_match(y, r, x, c), required_by(y, r, z);

    transitive_match(x, r, y) <-- requirement_match(x, r, y, c);
    transitive_match(x, r, z) <-- requirement_match(x, r, y, c), transitive_match(y, r, z);

    // querying
    relation query(EntityRef, QueryId, QueryType, ReqName, Symbol, bool);
    relation result(EntityRef, QueryId, NodeName, bool);

    // rules for generating for each query type:

    // include self in result
    result(r, q_id + 1, s ,last) <--
        query(r, q_id, qt, _, sym("SELF"), last) if *qt != QueryType::PropSource,
        result(r, q_id, s, false);

    result(r, q_id + 1, t ,last) <-- transitive_match(s, a, t),
        query(r, q_id, QueryType::TransitiveRelation, a, _, last),
        result(r, q_id, s, false);

    result(r, q_id + 1, s, last) <-- required_by(s, a, t),
              query(r, q_id, QueryType::RequiredBy, a, _, last),
              result(r, q_id, t, false);

    result(EntityRef::Relationship(n.clone(), r2.clone()), q_id + 1, s, last) <-- required_by(s, r2, t),
        query(entity_ref, q_id, QueryType::RequiredByType, a, _, last),
        relationship(n, r, a) if entity_ref.is_relationship(n, r),
        result(entity_ref, q_id, t, false);

    result(EntityRef::Relationship(n.clone(), r2.clone()), q_id + 1, t ,last) <-- transitive_match(s, r2, t),
        query(entity_ref, q_id, QueryType::TransitiveRelationType, a, _, last),
        relationship(n, r, a) if entity_ref.is_relationship(n, r),
        result(entity_ref, q_id, s, false);

    result(r, q_id + 1, source, last) <-- requirement_match(source, a, target, ?cap),
        query(r, q_id, QueryType::Sources, a, _, last),
        result(r, q_id, target, false);

    result(r, q_id + 1, target, last) <-- requirement_match(source, a, target, ?cap),
        query(r, q_id, QueryType::Targets, a, _, last),
        result(r, q_id, source, false);

    // find the node that is the source of the given property
    result(r, q_id + 1, t, last) <-- property_source(current, cap, prop_name, t),
        query(r, q_id, QueryType::PropSource, prop_name, cap, last),
        result(r, q_id, current, false);

    // when property_expr query finishes with a target node, update property_value and property_source
    property_source(node_name, cap, prop_name, target) <--
       property_expr(node_name, cap, sym(""), prop_name, query_key),
       result(query_key, _, target, true);

    property_value(node_name, cap, sym(""), prop_name, value) <--
      property_expr(node_name, cap, sym(""), prop_name, query_key),
      result(query_key, q_id, target, true),
      query(query_key, q_id, QueryType::PropSource, target_prop, target_cap, true),
      property_value(target, target_cap, sym(""), target_prop, value);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[allow(clippy::field_reassign_with_default)]
    pub fn make_topology() -> Topology {
        let mut prog = Topology::default();
        prog.node = vec![("n1".into(), "Root".into())];
        prog.requirement_match = vec![
            (sym("n1"), sym("host"), sym("n2"), sym("feature")),
            (sym("n2"), sym("host"), sym("n3"), sym("feature")),
            (sym("n3"), sym("connect"), sym("n4"), sym("feature")),
        ];
        prog.run();
        prog
    }

    fn tvalue_lessthan(a: SimpleValue, b: SimpleValue) -> bool {
        a < b
    }

    #[test]
    fn test_tvalue() {
        assert!(!tvalue_lessthan(
            SimpleValue::integer { v: 1 },
            SimpleValue::string { v: "ssss".into() }
        ));
        assert!(tvalue_lessthan(
            SimpleValue::integer { v: 1 },
            SimpleValue::integer { v: 2 }
        ));

        let range = Constraint::in_range {
            v: ToscaValue::from((1, 4)),
        };
        assert!(range.matches(&ToscaValue::from(1)).unwrap());
        assert!(!range.matches(&ToscaValue::from(6)).unwrap());
    }

    #[test]
    fn test_make_topology() {
        let prog = make_topology();

        // test transitive closure by relationship
        assert_eq!(
            prog.transitive_match,
            [
                (sym("n1"), sym("host"), sym("n2")),
                (sym("n2"), sym("host"), sym("n3")),
                (sym("n3"), sym("connect"), sym("n4")),
                (sym("n1"), sym("host"), sym("n3")),
            ]
        );

        // test reverse transitive closure by relationship
        assert_eq!(
            prog.required_by,
            [
                (sym("n2"), sym("host"), sym("n1")),
                (sym("n3"), sym("host"), sym("n2")),
                (sym("n4"), sym("connect"), sym("n3")),
                (sym("n3"), sym("host"), sym("n1")),
            ]
        );
    }
}
