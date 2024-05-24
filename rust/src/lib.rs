use ascent::hashbrown::HashMap;
use log::debug;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;

mod topology;

pub use topology::{
    sym, Constraint, Criteria, CriteriaTerm, EntityRef, Field, FieldValue, QueryType, SimpleValue,
    Topology, ToscaValue,
};

/// A partial representations of a TOSCA node template (enough for [solve()])
#[derive(FromPyObject)]
pub struct Node {
  /// node template name
  pub name: String,
  /// TOSCA type of the template
  pub tosca_type: String,
  /// properties, capabilities, requirements
  pub fields: Vec<Field>,
}

type ToscaTypes = HashMap<String, Vec<String>>;

fn get_types(tosca_type: String, type_parents: &ToscaTypes) -> Vec<String> {
    match type_parents.get(&tosca_type) {
        Some(parents) => parents.clone(),
        None => vec![tosca_type],
    }
}

/// Add the given node to the Ascent program modeling a topology.
///
/// # Errors
///
/// This function will return a PyTypeError if a field is of an unexpected tosca type.
fn add_node_to_topology(node: Node, topology: &mut Topology, type_parents: &ToscaTypes) -> Result<(), PyErr> {
    let name = node.name;
    for tosca_type in get_types(node.tosca_type, type_parents) {
        topology.node.push((sym(&name), sym(&tosca_type)));
    }

    for f in node.fields {
        match f.value {
            FieldValue::Property { value } => {
                topology
                    .property_value
                    .push((name.clone(), None, f.name, value))
            }
            FieldValue::Capability {
                tosca_type,
                properties,
            } => {
                let entityref = EntityRef::Capability(format!("{name}__{cap}", cap = f.name));
                topology
                    .capability
                    .push((sym(&name), sym(&f.name), entityref.clone()));
                for tosca_type in get_types(tosca_type, type_parents) {
                    topology.entity.push((entityref.clone(), tosca_type));
                }
                for field in properties {
                    match field.value {
                        FieldValue::Property { value } => topology.property_value.push((
                            name.clone(),
                            Some(sym(&f.name)),
                            field.name,
                            value,
                        )),
                        _ => {
                            return Err(PyErr::new::<PyTypeError, _>(
                                "This field must be a TOSCA property",
                            ))
                        }
                    }
                }
            }
            FieldValue::Requirement { terms, tosca_type } => {
                let mut criteria = Criteria::default();
                for term in terms.iter() {
                    criteria.0.insert(term.clone());
                    match term {
                        CriteriaTerm::NodeName { n } => {
                            topology.req_term_node_name.push((
                                sym(&name),
                                sym(&f.name),
                                term.clone(),
                                sym(n),
                            ));
                        }
                        CriteriaTerm::NodeType { n } => {
                            topology.req_term_node_type.push((
                                sym(&name),
                                sym(&f.name),
                                term.clone(),
                                sym(n),
                            ));
                        }
                        CriteriaTerm::CapabilityName { n } => {
                            topology.req_term_cap_name.push((
                                sym(&name),
                                sym(&f.name),
                                term.clone(),
                                sym(n),
                            ));
                        }
                        CriteriaTerm::CapabilityTypeGroup { names } => {
                            for n in names {
                                topology.req_term_cap_type.push((
                                    sym(&name),
                                    sym(&f.name),
                                    term.clone(),
                                    sym(n),
                                ));
                            }
                        }
                        CriteriaTerm::PropFilter { n, capability, .. } => {
                            topology.req_term_prop_filter.push((
                                sym(&name),
                                sym(&f.name),
                                term.clone(),
                                capability.clone(),
                                sym(n),
                            ));
                        }
                        CriteriaTerm::NodeMatch { query } => {
                            topology
                                .result
                                .push((sym(&name), sym(&f.name), 0, sym(&name), false));

                            for (index, (q, n)) in query.iter().enumerate() {
                                topology.query.push((
                                    sym(&name),
                                    sym(&f.name),
                                    index,
                                    *q,
                                    sym(n),
                                    index + 1 == query.len(),
                                ));
                            }

                            topology.req_term_query.push((
                                sym(&name),
                                sym(&f.name),
                                term.clone(),
                                query.len(), // match the last result
                            ));
                        }
                    }
                }
                topology
                    .requirement
                    .push((name.clone(), f.name.clone(), criteria));

                if let Some(rel_type) = tosca_type {
                    for tosca_type in get_types(rel_type, type_parents) {
                        topology
                            .relationship
                            .push((name.clone(), f.name.clone(), tosca_type));
                    }
                }
            } // _ => continue,
        }
        // XXX add "feature" capability + entity ?
    }
    Ok(())
}

/// (source_node_name, req_name) => [(target_node_name, target_capability_name)]
pub type RequirementMatches = HashMap<(String, String), Vec<(String, String)>>;

/// A Python function that finds missing requirements for the given topology.
///
/// # Errors
///
/// This function will return an error if the topology can't converted to an Ascent program.
#[pyfunction]
pub fn solve(_topology: Vec<Node>, type_parents: ToscaTypes) -> PyResult<RequirementMatches> {
    let mut prog = Topology::default();
    for e in _topology {
        add_node_to_topology(e, &mut prog, &type_parents)?;
    }
    debug!("solving queries {:?}, {:?}", prog.req_term_query, prog.query);
    prog.run();
    debug!("solve results {:?}", prog.result);
    // return requirement_match
    let mut requirements = RequirementMatches::new();
    for (source_node_name, req_name, target_node_name, target_capability_name) in
        prog.requirement_match.into_iter()
    {
        if let Some(x) = requirements.get_mut(&(source_node_name.clone(), req_name.clone())) {
            x.push((target_node_name, target_capability_name));
        } else {
            requirements.insert(
                (source_node_name, req_name),
                vec![(target_node_name, target_capability_name)],
            );
        }
    }
    // XXX: return computed capabilities and property values
    Ok(requirements)
}

/// A Python module implemented in Rust.
#[allow(deprecated)]
#[pymodule]
fn tosca_solver(_py: Python, m: &PyModule) -> PyResult<()> {
    pyo3_log::init();
    m.add_function(wrap_pyfunction!(solve, m)?)?;
    m.add_class::<CriteriaTerm>()?;
    m.add_class::<SimpleValue>()?;
    m.add_class::<ToscaValue>()?;
    m.add_class::<Constraint>()?;
    m.add_class::<Field>()?;
    m.add_class::<FieldValue>()?;
    m.add_class::<QueryType>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_node(name: &str, connects_to: Option<&str>) -> Node {
        Node {
            name: name.into(),
            tosca_type: "Service".into(),
            fields: vec![
                // Field { name: "feature",
                //         value: FieldValue::Capability {
                //           tosca_type: "tosca.capabilities.Node",
                //           properties: vec![] }
                //         },
                // Field { name: "url_scheme",
                //         value: Property {
                //           value: ToscaValue { t: None,
                //                 v: string { v: "https" } } } },
                Field {
                    name: "parent".into(),
                    value: FieldValue::Requirement {
                        terms: vec![
                            CriteriaTerm::NodeType {
                                n: "Service".into(),
                            },
                            CriteriaTerm::NodeMatch {
                                query: vec![(QueryType::Sources, "connects_to".into())],
                            },
                        ],
                        tosca_type: Some("tosca.relationships.Root".into()),
                    },
                },
                Field {
                    name: "connects_to".into(),
                    value: FieldValue::Requirement {
                        terms: {
                            if let Some(connects_to_node) = connects_to {
                                vec![CriteriaTerm::NodeName {
                                    n: connects_to_node.into(),
                                }]
                            } else {
                                vec![]
                            }
                        },
                        tosca_type: Some("unfurl.relationships.Configures".into()),
                    },
                },
            ],
        }
    }

    #[test]
    fn test_make_topology() {
        let nodes = vec![
            make_node("test.connection", None),
            make_node("test.service", Some("test.connection")),
        ];
        let result = solve(nodes, ToscaTypes::new()).expect("solved");
        let expected: RequirementMatches = [(
            ("test.service".into(), "connects_to".into()),
            vec![("test.connection".into(), "feature".into())],
        )]
        .iter()
        .cloned()
        .collect();
        assert!(result == expected);
    }
}
