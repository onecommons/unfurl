// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `unfurl._rust`: Unfurl's Rust code, as one Python extension module. Each
//! part is a cargo feature, so a build can leave any out.

use pyo3::prelude::*;

/// Adds one feature's functions and classes to the module.
type AddPart = fn(&Bound<'_, PyModule>) -> PyResult<()>;

/// The parts this build has.
const PARTS: &[AddPart] = &[
    #[cfg(feature = "solver")]
    add_solver,
];

#[pymodule]
#[pyo3(name = "_rust")]
fn unfurl_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    pyo3_log::init();
    for add in PARTS {
        add(m)?;
    }
    Ok(())
}

/// The TOSCA topology solver: see `unfurl/solver.py`.
#[cfg(feature = "solver")]
fn add_solver(m: &Bound<'_, PyModule>) -> PyResult<()> {
    use tosca_solver::{
        Constraint, CriteriaTerm, Field, FieldValue, QueryType, SimpleValue, ToscaValue,
    };
    m.add_function(wrap_pyfunction!(tosca_solver::solve, m)?)?;
    m.add_function(wrap_pyfunction!(tosca_solver::regex_match_exact, m)?)?;
    m.add_function(wrap_pyfunction!(tosca_solver::make_pattern_constraint, m)?)?;
    m.add_class::<CriteriaTerm>()?;
    m.add_class::<SimpleValue>()?;
    m.add_class::<ToscaValue>()?;
    m.add_class::<Constraint>()?;
    m.add_class::<Field>()?;
    m.add_class::<FieldValue>()?;
    m.add_class::<QueryType>()?;
    Ok(())
}
