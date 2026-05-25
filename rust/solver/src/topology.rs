// Copyright (c) 2024 Adam Souzis
// SPDX-License-Identifier: MIT
#![allow(clippy::let_unit_value)] // ignore for ascent!
#![allow(clippy::collapsible_if)] // ignore for ascent!
#![allow(clippy::clone_on_copy)] // ignore for ascent!
#![allow(clippy::unused_enumerate_index)] // ignore for ascent!
#![allow(clippy::type_complexity)] // ignore for ascent!
#![allow(clippy::mutable_key_type)] // ignore for ascent! (ok because Regex in constraint is ignored by hash and eq)

use ascent::{ascent, lattice::set::Set};
use regex::Regex;
use semver::{Version, VersionReq};
use std::convert::From;
use std::{
    cmp::Ordering,
    collections::BTreeMap,
    fmt::Debug,
    hash::{Hash, Hasher},
};

#[cfg(feature = "python")]
use pyo3::prelude::*;

/// Wrapper around Regex that can be used with PyO3
/// The Regex field is not exposed to Python - only the pattern string is accessible
#[cfg_attr(feature = "python", pyclass)]
#[derive(Clone, Debug)]
pub struct CompiledPattern {
    pattern: String,
    compiled: Regex,
}

impl CompiledPattern {
    pub fn new(pattern: String) -> Result<Self, regex::Error> {
        let compiled = Regex::new(&pattern)?;
        Ok(CompiledPattern { pattern, compiled })
    }

    pub fn regex(&self) -> &Regex {
        &self.compiled
    }

    pub fn pattern(&self) -> &str {
        &self.pattern
    }
}

#[cfg(feature = "python")]
#[pymethods]
impl CompiledPattern {
    #[getter]
    fn get_pattern(&self) -> &str {
        &self.pattern
    }
}

impl PartialEq for CompiledPattern {
    fn eq(&self, other: &Self) -> bool {
        self.pattern == other.pattern
    }
}

impl Eq for CompiledPattern {}

impl Hash for CompiledPattern {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        self.pattern.hash(state);
    }
}

pub type Symbol<'a> = &'a str;

type EntityName<'a> = Symbol<'a>;
type NodeName<'a> = EntityName<'a>;
// type AnonEntityId<'a> = EntityName<'a>;
type CapabilityName<'a> = Symbol<'a>;
type PropName<'a> = Symbol<'a>;
type ReqName<'a> = Symbol<'a>;
pub type TypeName<'a> = Symbol<'a>;
type QueryId = usize;
type Query = Vec<(QueryType, String, String)>;

/// Represents the match criteria for a requirement.
///
/// Corresponds to "node", "capability", and "node_filter"
/// fields on a TOSCA requirement and "valid_target_types" on relationship types.
#[cfg_attr(feature = "python", pyclass(eq))]
#[derive(Clone, PartialOrd, Ord, PartialEq, Eq, Hash, Debug)]

pub enum CriteriaTerm {
    NodeName {
        n: String,
    },
    NodeType {
        n: String,
    },
    CapabilityName {
        n: String,
    },
    CapabilityTypeGroup {
        names: Vec<String>,
    },
    PropFilter {
        n: String,
        capability: Option<String>,
        constraints: Vec<Constraint>,
    },
    NodeMatch {
        start_node: String,
        query: Vec<(QueryType, String, String)>,
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
    EntityType,
}

/// Constraints used in node filters
#[allow(non_camel_case_types)]
#[cfg_attr(feature = "python", pyclass)]
#[derive(Clone, Debug)]
pub enum Constraint {
    equal {
        v: ToscaValue,
    },
    greater_than {
        v: ToscaValue,
    },
    greater_or_equal {
        v: ToscaValue,
    },
    less_than {
        v: ToscaValue,
    },
    less_or_equal {
        v: ToscaValue,
    },
    in_range {
        v: ToscaValue,
    },
    valid_values {
        v: ToscaValue,
    },
    length {
        v: ToscaValue,
    },
    min_length {
        v: ToscaValue,
    },
    max_length {
        v: ToscaValue,
    },
    version {
        v: ToscaValue,
    },
    pattern {
        v: ToscaValue,
        compiled: CompiledPattern,
    },
    // schema,  // XXX
}

impl PartialEq for Constraint {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Constraint::equal { v: v1 }, Constraint::equal { v: v2 }) => v1 == v2,
            (Constraint::greater_than { v: v1 }, Constraint::greater_than { v: v2 }) => v1 == v2,
            (Constraint::greater_or_equal { v: v1 }, Constraint::greater_or_equal { v: v2 }) => {
                v1 == v2
            }
            (Constraint::less_than { v: v1 }, Constraint::less_than { v: v2 }) => v1 == v2,
            (Constraint::less_or_equal { v: v1 }, Constraint::less_or_equal { v: v2 }) => v1 == v2,
            (Constraint::in_range { v: v1 }, Constraint::in_range { v: v2 }) => v1 == v2,
            (Constraint::valid_values { v: v1 }, Constraint::valid_values { v: v2 }) => v1 == v2,
            (Constraint::length { v: v1 }, Constraint::length { v: v2 }) => v1 == v2,
            (Constraint::min_length { v: v1 }, Constraint::min_length { v: v2 }) => v1 == v2,
            (Constraint::max_length { v: v1 }, Constraint::max_length { v: v2 }) => v1 == v2,
            (Constraint::version { v: v1 }, Constraint::version { v: v2 }) => v1 == v2,
            // For pattern, only compare the ToscaValue, ignore the compiled Regex
            (Constraint::pattern { v: v1, .. }, Constraint::pattern { v: v2, .. }) => v1 == v2,
            _ => false,
        }
    }
}

impl Eq for Constraint {}

impl Hash for Constraint {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        // Hash the discriminant first to distinguish between variants
        std::mem::discriminant(self).hash(state);

        match self {
            Constraint::equal { v } => v.hash(state),
            Constraint::greater_than { v } => v.hash(state),
            Constraint::greater_or_equal { v } => v.hash(state),
            Constraint::less_than { v } => v.hash(state),
            Constraint::less_or_equal { v } => v.hash(state),
            Constraint::in_range { v } => v.hash(state),
            Constraint::valid_values { v } => v.hash(state),
            Constraint::length { v } => v.hash(state),
            Constraint::min_length { v } => v.hash(state),
            Constraint::max_length { v } => v.hash(state),
            Constraint::version { v } => v.hash(state),
            // For pattern, only hash the ToscaValue, ignore the compiled Regex
            Constraint::pattern { v, .. } => v.hash(state),
        }
    }
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
            Constraint::version { v } => v,
            Constraint::pattern { v, .. } => v,
        }
    }

    pub fn matches(&self, t: &ToscaValue) -> Option<bool> {
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
            Constraint::version {
                v:
                    ToscaValue {
                        v: SimpleValue::string { v: req_str },
                        ..
                    },
            } => {
                let version_str = match &t.v {
                    SimpleValue::string { v } => v.clone(),
                    SimpleValue::integer { v } => v.to_string(),
                    SimpleValue::float { v } => v.0.to_string(),
                    _ => return Some(false), // other types can't be semver compatible
                };

                // Strip leading "v" if present (e.g., "v1.2.0" -> "1.2.0")
                let version_str = version_str.strip_prefix('v').unwrap_or(&version_str);

                // Pad partial versions to full semver format (e.g., "1" -> "1.0.0")
                let full_version = if version_str.matches('.').count() == 0 {
                    format!("{}.0.0", version_str)
                } else if version_str.matches('.').count() == 1 {
                    format!("{}.0", version_str)
                } else {
                    version_str.to_string()
                };

                match (VersionReq::parse(req_str), Version::parse(&full_version)) {
                    (Ok(version_req), Ok(version)) => Some(version_req.matches(&version)),
                    _ => Some(req_str == version_str), // non-semver version strings must match exactly
                }
            }
            Constraint::pattern { compiled, .. } => {
                // Only match against string values, use compiled regex for exact matching
                match &t.v {
                    SimpleValue::string { v: text } => {
                        // Use the compiled Regex to perform exact match
                        match compiled.regex().find(text) {
                            Some(m) => Some(m.start() == 0 && m.end() == text.len()),
                            None => Some(false),
                        }
                    }
                    _ => Some(false), // Non-string values don't match patterns
                }
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

#[inline]
fn match_criteria(full: &Criteria, current: &Criteria) -> bool {
    full == current
}

/// `f64` wrapper that defines a total equality, ordering, and hash so it can
/// be used inside types that need `Eq`/`Hash`. NaN is treated as equal to
/// NaN (consistent with hashing the bit pattern), and `total_cmp` provides a
/// total order over all `f64` values including NaN.
#[derive(Copy, Clone, Debug, Default)]
pub struct OrderedF64(pub f64);

impl PartialEq for OrderedF64 {
    fn eq(&self, other: &Self) -> bool {
        self.0.to_bits() == other.0.to_bits()
    }
}

impl Eq for OrderedF64 {}

impl Hash for OrderedF64 {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.0.to_bits().hash(state);
    }
}

impl PartialOrd for OrderedF64 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for OrderedF64 {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0.total_cmp(&other.0)
    }
}

impl From<f64> for OrderedF64 {
    fn from(v: f64) -> Self {
        OrderedF64(v)
    }
}

impl From<OrderedF64> for f64 {
    fn from(v: OrderedF64) -> Self {
        v.0
    }
}

#[cfg(feature = "python")]
impl<'py> IntoPyObject<'py> for OrderedF64 {
    type Target = pyo3::types::PyFloat;
    type Output = Bound<'py, pyo3::types::PyFloat>;
    type Error = std::convert::Infallible;
    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        self.0.into_pyobject(py)
    }
}

#[cfg(feature = "python")]
impl<'py> IntoPyObject<'py> for &OrderedF64 {
    type Target = pyo3::types::PyFloat;
    type Output = Bound<'py, pyo3::types::PyFloat>;
    type Error = std::convert::Infallible;
    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        self.0.into_pyobject(py)
    }
}

#[cfg(feature = "python")]
impl<'py> FromPyObject<'py> for OrderedF64 {
    fn extract_bound(ob: &Bound<'py, PyAny>) -> PyResult<Self> {
        Ok(OrderedF64(ob.extract::<f64>()?))
    }
}

/// Simple TOSCA value
#[allow(non_camel_case_types)]
#[cfg_attr(feature = "python", pyclass(eq, ord))]
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum SimpleValue {
    // tosca simple values
    integer { v: i128 },
    string { v: String },
    boolean { v: bool },
    float { v: OrderedF64 },
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

impl Hash for SimpleValue {
    #[inline]
    fn hash<H: Hasher>(&self, state: &mut H) {
        let tag = std::mem::discriminant(self);
        Hash::hash(&tag, state);
        match self {
            SimpleValue::integer { v } => Hash::hash(v, state),
            SimpleValue::string { v } => Hash::hash(v, state),
            SimpleValue::boolean { v } => Hash::hash(v, state),
            SimpleValue::float { v } => Hash::hash(v, state),
            SimpleValue::list { v } => Hash::hash(v, state),
            SimpleValue::range { v } => Hash::hash(v, state),
            SimpleValue::map { v } => Hash::hash(v, state),
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
sv_from!(OrderedF64, float);
impl From<f64> for SimpleValue {
    fn from(item: f64) -> Self {
        SimpleValue::float {
            v: OrderedF64(item),
        }
    }
}
sv_from!(bool, boolean);
sv_from!(String, string);
sv_from!((i128, i128), range);
sv_from!(Vec<ToscaValue>, list);
sv_from!(BTreeMap<String, ToscaValue>, map);

/// A TOSCA value. If a complex value or typed scalar, type_name will be set.
#[cfg_attr(feature = "python", pyclass(eq, ord))]
#[derive(Clone, PartialOrd, PartialEq, Eq, Hash, Debug)]
pub struct ToscaValue {
    // `v` comes before `type_name` so the derived ordering compares the
    // underlying value first; `type_name` is a tie-breaker.
    #[cfg(feature = "python")]
    #[pyo3(get)]
    pub v: SimpleValue,

    #[cfg(not(feature = "python"))]
    pub v: SimpleValue,

    #[cfg(feature = "python")]
    #[pyo3(get, set)]
    pub type_name: Option<String>,

    #[cfg(not(feature = "python"))]
    pub type_name: Option<String>,
}

#[cfg(feature = "python")]
#[pymethods]
impl ToscaValue {
    #[new]
    #[pyo3(signature = (value, name=None))]
    fn new(value: SimpleValue, name: Option<String>) -> Self {
        ToscaValue {
            type_name: name,
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
#[cfg_attr(feature = "python", pyclass(eq))]
#[derive(Clone, PartialOrd, PartialEq, Eq, Hash, Debug)]
pub enum FieldValue {
    Property {
        value: Option<ToscaValue>,
        computed: Option<(String, Query)>,
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
#[cfg_attr(feature = "python", pyclass(eq, ord))]
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

#[cfg_attr(feature = "python", pymethods)]
impl Field {
    #[cfg(feature = "python")]
    #[new]
    fn new(name: String, value: FieldValue) -> Self {
        Field { name, value }
    }

    #[cfg(feature = "python")]
    #[setter]
    fn set_value(&mut self, value: FieldValue) -> PyResult<()> {
        self.value = value;
        Ok(())
    }

    fn __repr__(&self) -> String {
        format!("{self:?}")
    }

    pub fn has_field_type(&self, value: &FieldValue) -> bool {
        std::mem::discriminant(&self.value) == std::mem::discriminant(value)
    }
}

#[derive(Clone, PartialEq, Eq, Hash, Debug)]
pub enum EntityRef<'a> {
    Node(NodeName<'a>),
    Capability(NodeName<'a>, CapabilityName<'a>),
    Relationship(NodeName<'a>, ReqName<'a>),
    Property(NodeName<'a>, CapabilityName<'a>, PropName<'a>),
    // DataEntity(AnonEntityId<'a>),
}

impl<'a> EntityRef<'a> {
    pub fn is_relationship(&self, node_name: &NodeName<'a>, req_name: &ReqName<'a>) -> bool {
        matches!(self, Self::Relationship(n, r) if *n == *node_name && *r == *req_name)
    }

    pub fn is_capability(&self, node_name: &NodeName<'a>, cap_name: &CapabilityName<'a>) -> bool {
        matches!(self, Self::Capability(n, cap) if *n == *node_name && *cap == *cap_name)
    }

    /// Extract the node name
    pub fn node_name(&self) -> NodeName<'a> {
        match self {
            Self::Node(n) => n,
            Self::Capability(n, _) => n,
            Self::Relationship(n, _) => n,
            Self::Property(n, ..) => n,
        }
    }

    pub fn req_name(&self) -> Option<ReqName<'a>> {
        match self {
            Self::Relationship(_, r) => Some(*r),
            _ => None,
        }
    }
}

fn choose_cap<'a>(
    a: Option<CapabilityName<'a>>,
    b: Option<CapabilityName<'a>>,
) -> Option<CapabilityName<'a>> {
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
    pub(crate) struct Topology<'a>;

    relation entity(EntityRef<'a>, TypeName<'a>);
    relation node(NodeName<'a>, TypeName<'a>);
    relation live(NodeName<'a>, CapabilityName<'a>, bool);

    // reqname is set if property is on a relationship template
    // final bool is true when set by property_expr match
    relation property_value (NodeName<'a>, CapabilityName<'a>, ReqName<'a>, PropName<'a>, ToscaValue, bool);
    // if property is referenced in a node_filter match:
    // translate computed property's eval expression into a query
    relation property_expr (NodeName<'a>, CapabilityName<'a>, ReqName<'a>, PropName<'a>, EntityRef<'a>);
    // otherwise if property is not computed, add property_source(current, cap, prop_name, current)
    relation property_source (NodeName<'a>, CapabilityName<'a>, PropName<'a>, NodeName<'a>);

    // node_template definition
    relation capability (NodeName<'a>, CapabilityName<'a>, EntityRef<'a>);
    relation requirement(NodeName<'a>, ReqName<'a>, Criteria);
    relation relationship(NodeName<'a>, ReqName<'a>, TypeName<'a>);
    relation req_term_node_name(NodeName<'a>, ReqName<'a>, CriteriaTerm, NodeName<'a>);
    relation req_term_node_type(NodeName<'a>, ReqName<'a>, CriteriaTerm, TypeName<'a>);
    relation req_term_cap_type(NodeName<'a>, ReqName<'a>, CriteriaTerm, TypeName<'a>);
    relation req_term_cap_name(NodeName<'a>, ReqName<'a>, CriteriaTerm, CapabilityName<'a>);
    relation req_term_prop_filter(NodeName<'a>, ReqName<'a>, CriteriaTerm, CapabilityName<'a>, PropName<'a>);
    relation req_term_query(NodeName<'a>, ReqName<'a>, CriteriaTerm, QueryId);
    relation term_match(NodeName<'a>, ReqName<'a>, Criteria, CriteriaTerm, NodeName<'a>, Option<CapabilityName<'a>>);
    lattice filtered(NodeName<'a>, ReqName<'a>, NodeName<'a>, Option<CapabilityName<'a>>, Criteria, Criteria);
    relation requirement_match(NodeName<'a>, ReqName<'a>, NodeName<'a>, CapabilityName<'a>);
    // for conditional nodes:
    lattice live_filter(NodeName<'a>, Set<ReqName<'a>>, Set<ReqName<'a>>);
    relation missing_requirements(NodeName<'a>, Set<ReqName<'a>>);

    term_match(source, req, criteria, ct, target, None) <--
        node(target, typename), requirement(source, req, criteria),
        live(target, "", true),
        req_term_node_name(source, req, ct, target) if source != target;

    term_match(source, req, criteria, ct, target, None) <--
        node(target, typename), requirement(source, req, criteria),
        live(target, "", true),
        req_term_node_type(source, req, ct, typename) if source != target;

    term_match(source, req, criteria, ct, target, Some(cap_name.clone())) <--
        capability(target, cap_name, cap_id), entity(cap_id, typename),
        requirement(source, req, criteria),
        live(target, "", true),
        // live(target, cap_name, true)
        req_term_cap_type(source, req, ct, typename) if source != target;

    term_match(source, req, criteria, ct, target, Some(cap_name.clone())) <--
        capability(target, cap_name, _), requirement(source, req, criteria),
        term_match(source, req, criteria, _, target, _),  // only match req_term_capname after we found candidate target nodes
        live(target, "", true),
        // live(target, cap_name, true)
        req_term_cap_name(source, req, ct, cap_name);

    term_match(source, req, criteria, ct, target, None) <--
        property_value(target, capname, "", propname, value, ?computed),
        requirement(source, req, criteria),
        live(target, "", true),
        // live(target, capname, true)
        req_term_prop_filter(source, req, ct, capname, propname) if source != target && ct.match_property(value);

    // for node filters with capability typename instead of capability name:
    term_match(source, req, criteria, ct, target, None) <--
        property_value(target, capname, "", propname, value, ?computed),
        requirement(source, req, criteria),
        capability(target, capname, cap_id), entity(cap_id, typename),
        live(target, "", true),
        // live(target, capname, true)
        req_term_prop_filter(source, req, ct, typename, propname) if source != target && ct.match_property(value);

    term_match(source, req, criteria, ct, target, None) <--
        result(entity_ref, q_id, target, true),
        req_term_query(source, req, ct, q_id) if entity_ref.is_relationship(source, req),
        live(target, "", true),
        requirement(source, req, criteria);

    filtered(name, req_name, target, cn, criteria, Criteria::singleton(term.clone())) <--
        term_match(name, req_name, criteria, term, target, cn);

    filtered(name, req_name, target, choose_cap(tcn.clone(), fcn.clone()), criteria,
            Set({let mut fc = f.0.clone(); fc.insert(term.clone()); fc})) <--
        term_match(name, req_name, criteria, term, target, tcn),
        filtered(name, req_name, target, fcn, criteria, ?f);

    // if all the criteria have been found, create a requirement_match
    requirement_match(name, req_name, target, fcn.clone().unwrap_or("feature")) <--
        filtered(name, req_name, target, fcn, criteria, filter) if match_criteria(filter, criteria);

    // live(extract_node(source), extract_cap(source), true)) <-- requirement_match(source, sym("~DYNCAP"), target, target_cap);

    // update set of found requirements
    live_filter(node_name, requirements, Set::<ReqName<'a>>::singleton(req_name)) <--
        requirement_match(node_name, req_name, _, _),
        missing_requirements(node_name, requirements) if requirements.contains(req_name);

    live_filter(node_name, requirements, Set({let mut fc = f.0.clone(); fc.insert(req_name); fc})) <--
        requirement_match(node_name, req_name, _, _),
        missing_requirements(node_name, requirements) if requirements.contains(req_name),
        live_filter(node_name, ?f, requirements);

    live(node_name, "", true) <-- live_filter(node_name, filter, requirements) if filter == requirements;

    // graph navigation
    relation required_by(NodeName<'a>, ReqName<'a>, NodeName<'a>);
    relation transitive_match(NodeName<'a>, ReqName<'a>, NodeName<'a>);

    required_by(y, r, x) <-- requirement_match(x, r, y, c);
    required_by(x, r, z) <-- requirement_match(y, r, x, c), required_by(y, r, z);

    transitive_match(x, r, y) <-- requirement_match(x, r, y, c);
    transitive_match(x, r, z) <-- requirement_match(x, r, y, c), transitive_match(y, r, z);

    // querying
    // bool indicates whether the query or result is last in the query chain
    // entityref is a relationship or a property
    relation query(EntityRef<'a>, QueryId, QueryType, ReqName<'a>, Symbol<'a>, bool);
    relation result(EntityRef<'a>, QueryId, NodeName<'a>, bool);

    // rules for generating for each query type:

    // include self in result
    result(r, q_id + 1, s, last) <--
        query(r, q_id, qt, _, "SELF", last) if *qt != QueryType::PropSource,
        result(r, q_id, s, false);

    result(r, q_id + 1, s, last) <-- node(s, t),
        query(r, q_id, QueryType::EntityType, t, _, last),
        result(r, q_id, s, false);

    result(r, q_id + 1, t, last) <-- transitive_match(s, a, t),
        query(r, q_id, QueryType::TransitiveRelation, a, _, last),
        result(r, q_id, s, false);

    result(r, q_id + 1, t, last) <-- transitive_match(s, a, t),
        query(r, q_id, QueryType::TransitiveRelationType, rel_type, _, last),
        relationship(t, ?req, ret_type),  //any req that matches the type
        result(r, q_id, s, false);

    result(r, q_id + 1, s, last) <-- required_by(s, a, t),
              query(r, q_id, QueryType::RequiredBy, a, _, last),
              result(r, q_id, t, false);

    result(r, q_id + 1, s, last) <-- required_by(s, a, t),
              query(r, q_id, QueryType::RequiredByType, rel_type, _, last),
              relationship(s, ?req, rel_type), //any req that matches the type
              result(r, q_id, t, false);

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
    // property_expr found a result, set property_source to the target
    property_source(node_name, cap, prop_name, target) <--
       property_expr(node_name, cap, "", prop_name, query_key),
       result(query_key, _, target, true);

    // in this context (a property expression with a PropSource as last term), PropSource selects the property value from target
    property_value(node_name, cap, "", prop_name, value, true) <--
      property_expr(node_name, cap, "", prop_name, query_key),
      query(query_key, q_id, QueryType::PropSource, target_prop, target_cap, true),
      result(query_key, q_id + 1, target, true),
      property_value(target, target_cap, "", target_prop, value, ?computed);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[allow(clippy::field_reassign_with_default)]
    pub fn make_topology() -> Topology<'static> {
        let mut prog = Topology::default();
        prog.node = vec![("n1", "Root")];
        prog.requirement_match = vec![
            ("n1", "host", "n2", "feature"),
            ("n2", "host", "n3", "feature"),
            ("n3", "connect", "n4", "feature"),
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
    fn test_tvalue_float_eq() {
        use std::collections::hash_map::DefaultHasher;

        fn assert_eq_bound<T: Eq>(_: &T) {}

        let a = SimpleValue::float { v: OrderedF64(1.5) };
        let b = SimpleValue::float { v: OrderedF64(1.5) };
        let c = SimpleValue::float { v: OrderedF64(2.5) };
        let nan1 = SimpleValue::float {
            v: OrderedF64(f64::NAN),
        };
        let nan2 = SimpleValue::float {
            v: OrderedF64(f64::NAN),
        };

        // Eq holds (compiler check) and PartialEq agrees on equal/unequal pairs.
        assert_eq_bound(&a);
        assert_eq!(a, b);
        assert_ne!(a, c);

        // NaN is treated as equal to NaN — consistent with the Hash impl.
        assert_eq!(nan1, nan2);

        // Equal values hash the same.
        let mut h1 = DefaultHasher::new();
        let mut h2 = DefaultHasher::new();
        a.hash(&mut h1);
        b.hash(&mut h2);
        assert_eq!(h1.finish(), h2.finish());

        // From<f64> still produces an equivalent SimpleValue::float.
        assert_eq!(SimpleValue::from(1.5_f64), a);

        // Total ordering on floats; NaN sorts after finite values.
        assert!(a < c);
        assert!(c < nan1);

        // SimpleValue can now live in a HashSet (requires Eq + Hash).
        let mut set = std::collections::HashSet::new();
        set.insert(a.clone());
        set.insert(b.clone());
        assert_eq!(set.len(), 1);
        set.insert(c);
        assert_eq!(set.len(), 2);
    }

    #[test]
    fn test_semver_compatible() {
        // Test version constraint with caret requirement "^1.2.0"
        let semver_constraint = Constraint::version {
            v: ToscaValue::from("^1.2".to_string()),
        };

        // Should match compatible versions within same major version
        assert!(semver_constraint
            .matches(&ToscaValue::from("1.2.0".to_string()))
            .unwrap());
        assert!(semver_constraint
            .matches(&ToscaValue::from("1.2.5".to_string()))
            .unwrap());
        assert!(semver_constraint
            .matches(&ToscaValue::from("1.9.1".to_string()))
            .unwrap());

        // Should not match different major versions
        assert!(!semver_constraint
            .matches(&ToscaValue::from("2.0.0".to_string()))
            .unwrap());
        assert!(!semver_constraint
            .matches(&ToscaValue::from("0.9.0".to_string()))
            .unwrap());

        // Should not match versions below the requirement
        assert!(!semver_constraint
            .matches(&ToscaValue::from("1.1.9".to_string()))
            .unwrap());

        // Test with integer and float values converted to strings
        // "1" -> "1.0.0" which is < "1.2.0", so should NOT match ^1.2.0
        assert!(!semver_constraint.matches(&ToscaValue::from(1)).unwrap()); // "1.0.0" < "1.2.0"
        assert!(!semver_constraint.matches(&ToscaValue::from(2)).unwrap()); // "2.0.0" is different major version

        // Test with float values
        assert!(semver_constraint.matches(&ToscaValue::from(1.3)).unwrap()); // "1.3" -> "1.3.0" matches ^1.2.0
        assert!(!semver_constraint.matches(&ToscaValue::from(1.1)).unwrap()); // "1.1" -> "1.1.0" < 1.2.0

        // Test with "v" prefix
        assert!(semver_constraint
            .matches(&ToscaValue::from("v1.2.5".to_string()))
            .unwrap());
        assert!(!semver_constraint
            .matches(&ToscaValue::from("v2.0.0".to_string()))
            .unwrap());

        // Test exact version requirement (no caret)
        let exact_constraint = Constraint::version {
            v: ToscaValue::from("= 1.2.0".to_string()),
        };

        assert!(exact_constraint
            .matches(&ToscaValue::from("1.2.0".to_string()))
            .unwrap());
        assert!(!exact_constraint
            .matches(&ToscaValue::from("1.2.5".to_string()))
            .unwrap());
        assert!(!exact_constraint
            .matches(&ToscaValue::from("1.9.1".to_string()))
            .unwrap());
        assert!(!exact_constraint
            .matches(&ToscaValue::from("2.0.0".to_string()))
            .unwrap());

        // Test tilde requirements
        let tilde_constraint = Constraint::version {
            v: ToscaValue::from("~1.2.3".to_string()),
        };

        // ~1.2.3 allows >=1.2.3, <1.3.0 (patch-level changes only)
        assert!(tilde_constraint
            .matches(&ToscaValue::from("1.2.3".to_string()))
            .unwrap());
        assert!(tilde_constraint
            .matches(&ToscaValue::from("1.2.9".to_string()))
            .unwrap());
        assert!(!tilde_constraint
            .matches(&ToscaValue::from("1.3.0".to_string()))
            .unwrap());
        assert!(!tilde_constraint
            .matches(&ToscaValue::from("1.1.9".to_string()))
            .unwrap());

        // Test tilde with major.minor (~1.2)
        let tilde_minor_constraint = Constraint::version {
            v: ToscaValue::from("~1.2".to_string()),
        };

        // ~1.2 allows >=1.2.0, <1.3.0
        assert!(tilde_minor_constraint
            .matches(&ToscaValue::from("1.2.0".to_string()))
            .unwrap());
        assert!(tilde_minor_constraint
            .matches(&ToscaValue::from("1.2.9".to_string()))
            .unwrap());
        assert!(!tilde_minor_constraint
            .matches(&ToscaValue::from("1.3.0".to_string()))
            .unwrap());

        let unsemver_constraint = Constraint::version {
            v: ToscaValue::from("branch".to_string()),
        };

        assert!(unsemver_constraint
            .matches(&ToscaValue::from("branch".to_string()))
            .unwrap());
        assert!(!unsemver_constraint
            .matches(&ToscaValue::from("1.2.9".to_string()))
            .unwrap());
    }

    #[test]
    fn test_pattern_constraint() {
        // Test pattern matching with valid patterns
        let email_pattern_str = r"^[a-z]+@[a-z]+\.[a-z]+$";
        let email_pattern = Constraint::pattern {
            v: ToscaValue::from(email_pattern_str.to_string()),
            compiled: CompiledPattern::new(email_pattern_str.to_string()).unwrap(),
        };

        assert!(email_pattern
            .matches(&ToscaValue::from("user@example.com".to_string()))
            .unwrap());
        assert!(!email_pattern
            .matches(&ToscaValue::from("invalid-email".to_string()))
            .unwrap());

        // Test digit pattern
        let digit_pattern_str = r"^\d{3}-\d{3}-\d{4}$";
        let digit_pattern = Constraint::pattern {
            v: ToscaValue::from(digit_pattern_str.to_string()),
            compiled: CompiledPattern::new(digit_pattern_str.to_string()).unwrap(),
        };

        assert!(digit_pattern
            .matches(&ToscaValue::from("123-456-7890".to_string()))
            .unwrap());
        assert!(!digit_pattern
            .matches(&ToscaValue::from("123-45-6789".to_string()))
            .unwrap());

        // Test pattern with non-string value (should return false)
        assert!(!email_pattern.matches(&ToscaValue::from(123)).unwrap());
        assert!(!email_pattern.matches(&ToscaValue::from(true)).unwrap());
    }

    #[test]
    fn test_make_topology() {
        let prog = make_topology();

        // test transitive closure by relationship
        assert_eq!(
            prog.transitive_match,
            [
                ("n1", "host", "n2"),
                ("n2", "host", "n3"),
                ("n3", "connect", "n4"),
                ("n1", "host", "n3"),
            ]
        );

        // test reverse transitive closure by relationship
        assert_eq!(
            prog.required_by,
            [
                ("n2", "host", "n1"),
                ("n3", "host", "n2"),
                ("n4", "connect", "n3"),
                ("n3", "host", "n1"),
            ]
        );
    }

    // ------------------------------------------------------------------
    // enum trait-impl coverage: hand-written PartialEq / Hash / get_value /
    // variant_id methods on Constraint / SimpleValue / CriteriaTerm that
    // weren't reached by the existing solver tests.
    // ------------------------------------------------------------------

    #[test]
    fn constraint_partial_eq_same_variant_compares_inner_value() {
        let a = Constraint::equal {
            v: ToscaValue::from(1),
        };
        let b = Constraint::equal {
            v: ToscaValue::from(1),
        };
        let c = Constraint::equal {
            v: ToscaValue::from(2),
        };
        assert_eq!(a, b);
        assert_ne!(a, c);
    }

    #[test]
    fn constraint_partial_eq_different_variants_never_match() {
        let v = ToscaValue::from(1);
        let ops: Vec<Constraint> = vec![
            Constraint::equal { v: v.clone() },
            Constraint::greater_than { v: v.clone() },
            Constraint::greater_or_equal { v: v.clone() },
            Constraint::less_than { v: v.clone() },
            Constraint::less_or_equal { v: v.clone() },
            Constraint::length { v: v.clone() },
            Constraint::min_length { v: v.clone() },
            Constraint::max_length { v: v.clone() },
        ];
        for (i, a) in ops.iter().enumerate() {
            for (j, b) in ops.iter().enumerate() {
                if i == j {
                    assert_eq!(a, b, "{i} should equal itself");
                } else {
                    assert_ne!(a, b, "{i} vs {j} should differ — different variants");
                }
            }
        }
    }

    #[test]
    fn constraint_pattern_eq_ignores_compiled_regex_struct() {
        // The two CompiledPatterns are separate instances; the Eq impl
        // should look at the ToscaValue only.
        let p = "abc".to_string();
        let a = Constraint::pattern {
            v: ToscaValue::from(p.clone()),
            compiled: CompiledPattern::new(p.clone()).unwrap(),
        };
        let b = Constraint::pattern {
            v: ToscaValue::from(p.clone()),
            compiled: CompiledPattern::new(p.clone()).unwrap(),
        };
        assert_eq!(a, b);
    }

    #[test]
    fn constraint_hash_distinguishes_variants_with_same_inner_value() {
        use std::collections::HashSet;
        let v = ToscaValue::from(1);
        let mut set = HashSet::new();
        set.insert(Constraint::equal { v: v.clone() });
        set.insert(Constraint::equal { v: v.clone() }); // dup
        assert_eq!(set.len(), 1);
        set.insert(Constraint::greater_than { v: v.clone() });
        set.insert(Constraint::less_than { v: v.clone() });
        assert_eq!(set.len(), 3, "variant discriminant must be hashed");
        set.insert(Constraint::equal {
            v: ToscaValue::from(2),
        }); // diff value, same variant
        assert_eq!(set.len(), 4);
    }

    #[test]
    fn constraint_get_value_extracts_inner_for_every_variant() {
        let v = ToscaValue::from(7);
        let p = "x".to_string();
        let pattern_v = ToscaValue::from(p.clone());
        let cases: Vec<Constraint> = vec![
            Constraint::equal { v: v.clone() },
            Constraint::greater_than { v: v.clone() },
            Constraint::greater_or_equal { v: v.clone() },
            Constraint::less_than { v: v.clone() },
            Constraint::less_or_equal { v: v.clone() },
            Constraint::in_range {
                v: ToscaValue::from((1, 10)),
            },
            Constraint::valid_values {
                v: ToscaValue::from(vec![v.clone()]),
            },
            Constraint::length { v: v.clone() },
            Constraint::min_length { v: v.clone() },
            Constraint::max_length { v: v.clone() },
            Constraint::version {
                v: ToscaValue::from("1.0".to_string()),
            },
            Constraint::pattern {
                v: pattern_v.clone(),
                compiled: CompiledPattern::new(p.clone()).unwrap(),
            },
        ];
        // get_value just returns &ToscaValue; verifying it doesn't panic and
        // returns the value we constructed with is enough to cover every arm.
        for c in &cases {
            let inner = c.get_value();
            match c {
                Constraint::in_range { v } => assert_eq!(inner, v),
                Constraint::valid_values { v } => assert_eq!(inner, v),
                Constraint::version { v } => assert_eq!(inner, v),
                Constraint::pattern { v, .. } => assert_eq!(inner, v),
                _ => assert_eq!(inner, &v),
            }
        }
    }

    #[test]
    fn constraint_matches_basic_comparison_ops() {
        let val = ToscaValue::from(5);
        assert_eq!(
            Constraint::equal {
                v: ToscaValue::from(5)
            }
            .matches(&val),
            Some(true)
        );
        assert_eq!(
            Constraint::equal {
                v: ToscaValue::from(6)
            }
            .matches(&val),
            Some(false)
        );
        assert_eq!(
            Constraint::greater_than {
                v: ToscaValue::from(3)
            }
            .matches(&val),
            Some(true)
        );
        assert_eq!(
            Constraint::greater_than {
                v: ToscaValue::from(5)
            }
            .matches(&val),
            Some(false)
        );
        assert_eq!(
            Constraint::greater_or_equal {
                v: ToscaValue::from(5)
            }
            .matches(&val),
            Some(true)
        );
        assert_eq!(
            Constraint::less_than {
                v: ToscaValue::from(10)
            }
            .matches(&val),
            Some(true)
        );
        assert_eq!(
            Constraint::less_or_equal {
                v: ToscaValue::from(5)
            }
            .matches(&val),
            Some(true)
        );
    }

    #[test]
    fn constraint_matches_valid_values() {
        let value_list = ToscaValue::from(vec![
            ToscaValue::from(1),
            ToscaValue::from(2),
            ToscaValue::from(3),
        ]);
        let c = Constraint::valid_values { v: value_list };
        assert_eq!(c.matches(&ToscaValue::from(2)), Some(true));
        assert_eq!(c.matches(&ToscaValue::from(7)), Some(false));
    }

    #[test]
    fn constraint_matches_length_against_string() {
        let s = ToscaValue::from("hello".to_string()); // len 5
        assert_eq!(
            Constraint::length {
                v: ToscaValue::from(5_i128)
            }
            .matches(&s),
            Some(true)
        );
        assert_eq!(
            Constraint::length {
                v: ToscaValue::from(4_i128)
            }
            .matches(&s),
            Some(false)
        );
    }

    #[test]
    fn constraint_matches_length_against_non_sized_value_returns_none() {
        // Integers have no `len`; matches returns None.
        let n = ToscaValue::from(42);
        assert_eq!(
            Constraint::length {
                v: ToscaValue::from(1_i128)
            }
            .matches(&n),
            None
        );
    }

    #[test]
    fn simple_value_variant_id_is_distinct_for_every_variant() {
        use std::collections::HashSet;
        let values: Vec<SimpleValue> = vec![
            SimpleValue::integer { v: 1 },
            SimpleValue::string { v: "x".into() },
            SimpleValue::boolean { v: true },
            SimpleValue::float { v: OrderedF64(1.0) },
            SimpleValue::list { v: vec![] },
            SimpleValue::range { v: (1, 2) },
            SimpleValue::map { v: BTreeMap::new() },
        ];
        let ids: HashSet<usize> = values.iter().map(SimpleValue::variant_id).collect();
        assert_eq!(ids.len(), values.len(), "every variant needs a unique id");
        // And the values are non-zero (the impl uses 1..=7).
        for v in &values {
            assert!(v.variant_id() >= 1);
        }
    }

    #[test]
    fn criteria_term_variant_id_is_distinct_for_every_variant() {
        use std::collections::HashSet;
        let terms: Vec<CriteriaTerm> = vec![
            CriteriaTerm::NodeName { n: "a".into() },
            CriteriaTerm::NodeType { n: "T".into() },
            CriteriaTerm::CapabilityName { n: "c".into() },
            CriteriaTerm::CapabilityTypeGroup {
                names: vec!["g".into()],
            },
            CriteriaTerm::PropFilter {
                n: "p".into(),
                capability: None,
                constraints: vec![],
            },
            CriteriaTerm::NodeMatch {
                start_node: "n".into(),
                query: vec![],
            },
        ];
        let ids: HashSet<usize> = terms.iter().map(CriteriaTerm::variant_id).collect();
        assert_eq!(ids.len(), terms.len());
    }
}
