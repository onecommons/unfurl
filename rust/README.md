# TOSCA-Solver

This crate infers the relationships between [TOSCA](https://docs.unfurl.run/tosca.html) node templates when given a set of node templates and their requirements.

Each TOSCA requirement is encoded as a set of constraints including:

* node and capability types
* the relationship's valid_target_types
* node_filter constraints
* node_filter match expressions

``solve()`` will return the nodes that match the requirements associated with a given set of nodes.

By default this crate is exposed as a Python extension module and is used by [Unfurl](https://github.com/onecommons/unfurl), but it could be used by any TOSCA 1.3 processor.

## Usage

By default, this crate is built as a Python extension, to disable this feature, set ``default-features = false``. The ``solve`` function is its main entry point and can invoked from the Rust or Python. Your TOSCA topology will have to be encoded as a HashMap of Nodes -- see https://github.com/onecommons/unfurl/blob/main/unfurl/solver.py for an example.

## Design notes

Finding the match for a TOSCA requirement with a node_filter can't be determined with a simple algorithm. For example, a requirement node_filter's match can depend on a property value that is itself computed from a requirement (e.g. via TOSCA's ``get_property`` function). So finding a node filter match might change a property's value, which in turn could affect the node_filter match.

In addition to this basic functionality, this solver will use type inference to resolve requirements that haven't defined explicit node targets and supports Unfurl's node_filter extensions for applying constraints and graph querying.

Luckily, by eschewing negation and quantification we can avoid the need for a full SAT solver and instead encode the inference rules as [Datalog](https://blogit.michelin.io/an-introduction-to-datalog/)-like query rules using [Ascent](https://github.com/s-arash/ascent/) -- a simpler and more scalable approach.

## Why?

One of Unfurl's goal is to enable adaptable blueprints that can easily compose independent, open-source components -- imagine something like a [package manager for the cloud](https://github.com/onecommons/cloudmap). This crate allows blueprints to express their requirements in a loosely-coupled, generic manner.

## Development

You can use the standard cargo commands for development if you use its ``--no-default-features`` flag to skip compiling with the "pyo3/extension-module" feature.

To compile the Python extension, in the parent of this directory, run:

``python setup.py build_rust --debug --inplace``

Run `pip install setuptools-rust>=1.7.0 pbr` to install `setup.py`'s requirements.

## Tests

Cargo tests should be invoked with ``cargo test --no-default-features`` because the "pyo3/extension-module" feature doesn't work with cargo test.

Unfurl's Python unit tests have more extensive tests in https://github.com/onecommons/unfurl/blob/main/tests/test_solver.py. See Unfurl's main README for instructions on running its unit tests.
