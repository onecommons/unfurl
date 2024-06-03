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
    pub has_restrictions: bool,
}

type ToscaTypes = HashMap<String, Vec<String>>;

fn get_types(tosca_type: &String, type_parents: &ToscaTypes) -> Vec<String> {
    match type_parents.get(tosca_type) {
        Some(parents) => parents.clone(),
        None => vec![tosca_type.clone()],
    }
}

fn add_field_to_topology(
    f: Field,
    topology: &mut Topology,
    node_name: &String,
    type_parents: &HashMap<String, Vec<String>>,
) -> Result<(), PyErr> {
    match f.value {
        FieldValue::Property { value } => {
            topology
                .property_value
                .push((sym(node_name), None, sym(&f.name), value))
        }
        FieldValue::Capability {
            tosca_type,
            properties,
        } => {
            let cap_name = format!("{node_name}__{cap}", cap = f.name);
            let entityref = EntityRef::Capability(cap_name);
            topology
                .capability
                .push((sym(node_name), sym(&f.name), entityref.clone()));
            for tosca_type in get_types(&tosca_type, type_parents) {
                topology.entity.push((entityref.clone(), tosca_type));
            }
            for field in properties {
                match field.value {
                    FieldValue::Property { value } => topology.property_value.push((
                        sym(node_name),
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
        FieldValue::Requirement {
            terms,
            tosca_type,
            restrictions,
        } => {
            let mut criteria = Criteria::default();
            for term in terms.iter() {
                criteria.0.insert(term.clone());
                match term {
                    CriteriaTerm::NodeName { n } => {
                        topology.req_term_node_name.push((
                            sym(node_name),
                            sym(&f.name),
                            term.clone(),
                            sym(n),
                        ));
                    }
                    CriteriaTerm::NodeType { n } => {
                        topology.req_term_node_type.push((
                            sym(node_name),
                            sym(&f.name),
                            term.clone(),
                            sym(n),
                        ));
                    }
                    CriteriaTerm::CapabilityName { n } => {
                        topology.req_term_cap_name.push((
                            sym(node_name),
                            sym(&f.name),
                            term.clone(),
                            sym(n),
                        ));
                    }
                    CriteriaTerm::CapabilityTypeGroup { names } => {
                        for n in names {
                            topology.req_term_cap_type.push((
                                sym(node_name),
                                sym(&f.name),
                                term.clone(),
                                sym(n),
                            ));
                        }
                    }
                    CriteriaTerm::PropFilter { n, capability, .. } => {
                        topology.req_term_prop_filter.push((
                            sym(node_name),
                            sym(&f.name),
                            term.clone(),
                            capability.clone(),
                            sym(n),
                        ));
                    }
                    CriteriaTerm::NodeMatch { query } => {
                        topology.result.push((
                            sym(node_name),
                            sym(&f.name),
                            0,
                            sym(node_name),
                            false,
                        ));

                        for (index, (q, n)) in query.iter().enumerate() {
                            topology.query.push((
                                sym(node_name),
                                sym(&f.name),
                                index,
                                *q,
                                sym(n),
                                index + 1 == query.len(),
                            ));
                        }

                        topology.req_term_query.push((
                            sym(node_name),
                            sym(&f.name),
                            term.clone(),
                            query.len(), // match the last result
                        ));
                    }
                }
            }
            topology
                .requirement
                .push((node_name.clone(), f.name.clone(), criteria, restrictions));

            if let Some(rel_type) = tosca_type {
                for tosca_type in get_types(&rel_type, type_parents) {
                    topology
                        .relationship
                        .push((node_name.clone(), f.name.clone(), tosca_type));
                }
            }
        } // _ => continue,
    }
    Ok(())
}

/// Add the given node to the Ascent program modeling a topology.
///
/// # Errors
///
/// This function will return a PyTypeError if a field is of an unexpected tosca type.
fn add_node_to_topology(
  node: &Node,
  topology: &mut Topology,
  type_parents: &ToscaTypes,
  req_only: bool,
  include_restrictions: bool,
) -> Result<(), PyErr> {
  let name = sym(&node.name);
  for tosca_type in get_types(&node.tosca_type, type_parents) {
      topology.node.push((sym(&name), sym(&tosca_type)));
  }
  for f in node.fields.iter() {
      if let FieldValue::Requirement { restrictions, .. } = &f.value {
          let has_restrictions = !restrictions.is_empty();
          if has_restrictions != include_restrictions || (!req_only && !include_restrictions) {
              continue; // include_restrictions ignores req_only == false
          }
      } else if req_only {
          continue; // not a requirement, skip
      }
      add_field_to_topology(f.clone(), topology, &name, type_parents)?;
      // XXX add "feature" capability + entity ?
  }
  Ok(())
}

fn add_matched_and_restricted_requirements(
    nodes: &HashMap<String, Node>,
    mut topology: Topology,
    type_parents: &ToscaTypes,
    start: usize,
) -> (Topology, usize) {
    let mut index = start;
    let matches = topology.requirement_match.clone();
    let requirements = topology.requirement_indices_0_1.0.clone();
    for (source_node_name, req_name, target_node_name, target_capability_name) in
        matches.iter().skip(start)
    {
        index += 1;
        let requirement_values = requirements
            .get(&(source_node_name.clone(), req_name.clone()))
            .expect("missing requirement");
        let (_, constraints) = &requirement_values[0];
        let n = nodes.get(target_node_name).expect("node not found!");
        for field in constraints {
            let req_field = n
                .fields
                .iter()
                .find(|f| f.name == field.name)
                .expect("missing field");
            // requirement shouldn't have been added to the topology yet
            // XXX add now with extra criteria now, add nested restrictions
            if let FieldValue::Requirement {
                terms,
                tosca_type: _,
                restrictions,
            } = &req_field.value
            {
                if let FieldValue::Requirement {
                    terms: restricted_terms,
                    tosca_type,
                    restrictions: restricted_restrictions,
                } = &field.value
                {
                    add_field_to_topology(
                        Field {
                            name: field.name.clone(),
                            value: FieldValue::Requirement {
                                terms: [terms.clone(), restricted_terms.clone()].concat(),
                                tosca_type: tosca_type.clone(),
                                restrictions: [
                                    restrictions.clone(),
                                    restricted_restrictions.clone(),
                                ]
                                .concat(),
                            },
                        },
                        &mut topology,
                        &n.name,
                        type_parents,
                    )
                    .expect("bad field");
                }
            }
        }
    }
    (topology, index)
}

/// (source_node_name, req_name) => [(target_node_name, target_capability_name)]
pub type RequirementMatches = HashMap<(String, String), Vec<(String, String)>>;

/// A Python function that finds missing requirements for the given topology.
///
/// Requirements can project additional terms on the matching node's requirements
/// We need to avoid the situation where a requirement finds a match before a projection is applied
/// to it since that match might not fulfill the projection's additional terms.
///
/// After solving round, for each new requirement_matches with projections: merge projections with requirement and add the requirement and req_term_*
/// for each req req Field, find the matching Field in the requirement and add the terms or add the Field if not found
///
/// So solve requirements with projections first:
/// 1. only add requirements (and their nodes) with projections (in case they match each other)
/// solve and
/// 2. add remaining nodes but omit requirements and req_term_*
/// 3. the new requirement_matches will have projections
/// 4. repeat 3 until no new matches
/// 5. add the remaining requirement and req_term_*
///
/// # Errors
///
/// This function will return an error if the topology can't converted to an Ascent program.
#[pyfunction]
pub fn solve(
    nodes: HashMap<String, Node>,
    type_parents: ToscaTypes,
) -> PyResult<RequirementMatches> {
    let mut prog = Topology::default();

    // add the constraining nodes but only with requirements with restrictions (in case they match each other)
    for node in nodes.values() {
        if node.has_restrictions {
            add_node_to_topology(node, &mut prog, &type_parents, false, true)?;
        }
    }
    prog.run();
    // update matched requirements
    let mut index = 0;
    (prog, index) = add_matched_and_restricted_requirements(&nodes, prog, &type_parents, index);

    // add remaining nodes but omit all requirements and req_term_*
    for node in nodes.values() {
        if !node.has_restrictions {
            add_node_to_topology(node, &mut prog, &type_parents, false, false)?;
        }
    }

    // keep searching for matches for restricted requirements
    loop {
        prog.run();
        (prog, index) = add_matched_and_restricted_requirements(&nodes, prog, &type_parents, index);
        if index == 0 {
            break;
        }
    }

    // no more restricted matches,
    // add the remaining requirement and req_term_* and finish the search
    for node in nodes.values() {
        add_node_to_topology(node, &mut prog, &type_parents, true, false)?;
    }
    prog.run();

    // return requirement_match
    let mut requirements = RequirementMatches::new();
    for (source_node_name, req_name, target_node_name, target_capability_name) in
        prog.requirement_match.clone()
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

    fn make_node(name: &str, connects_to: Option<&str>) -> (String, Node) {
        (
            name.into(),
            Node {
                name: name.into(),
                tosca_type: "Service".into(),
                has_restrictions: false,
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
                            restrictions: vec![],
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
                            restrictions: vec![],
                        },
                    },
                ],
            },
        )
    }

    #[test]
    fn test_make_topology() {
        let mut nodes = HashMap::<String, Node>::default();
        nodes.extend([
            make_node("test.connection", None),
            make_node("test.service", Some("test.connection")),
        ]);

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
