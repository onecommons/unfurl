# Copilot Instructions for Unfurl

## Project Overview

Unfurl is a command-line tool for deploying services and applications using the [OASIS TOSCA](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) (Topology and Orchestration Specification for Cloud Applications) standard. It creates deployment plans adapted to different environments (cloud providers, Kubernetes, self-hosted machines) and uses artifacts such as shell scripts, Terraform modules, or Ansible playbooks.

## Code Structure

- `unfurl/` - Main Python package containing the core implementation
  - `configurators/` - Integration implementations (Terraform, Ansible, Helm, etc.)
  - `server/` - Server-related functionality
  - `tosca_plugins/` - TOSCA parser plugins
  - `templates/` - Runtime templates
- `rust/` - Rust code for the TOSCA solver (compiled as a Python extension)
- `tosca-package/` - TOSCA DSL package for defining blueprints in Python
- `tosca-parser/` - Git submodule for TOSCA YAML parsing
- `tests/` - Test suite using pytest
- `docs/` - Sphinx documentation
- `docker/` - Docker configurations

## Build and Test Commands

### Python Testing

```bash
# Install tox for running tests
pip install tox==3.28.0

# Run tests with tox (preferred method)
tox -e py311 -v -- -vv

# Run tests directly with pytest
pytest tests/ -vv

# Run tests in parallel
pytest tests/ -vv -n auto --dist loadfile

# Run a specific test file
pytest tests/test_cli.py -vv

# Run tests with coverage
pytest tests/ --cov=unfurl --cov-report html --cov-report term
```

### Rust Testing

```bash
# Run Rust tests
cargo test --no-default-features --manifest-path rust/Cargo.toml

# Run Rust linter
cargo clippy --manifest-path rust/Cargo.toml
```

### Building

```bash
# Build the Rust extension (required before running Python code)
python setup.py build_rust --debug --inplace

# Build documentation
tox -e docs
```

### Type Checking

```bash
# Run mypy type checking
mypy unfurl --install-types --non-interactive
```

## Key Dependencies

- **Python**: 3.8+ (3.9-3.13 actively tested)
- **Rust**: Required for building the tosca_solver extension
- **Key Python packages**: 
  - `tosca` - TOSCA DSL (from tosca-package)
  - `ansible` - For Ansible configurator
  - `terraform` integration
  - `jinja2` - Templating

## Coding Conventions

- Follow PEP 8 style guidelines for Python code
- Use type hints where practical
- Preserve existing code structure and patterns
- YAML configuration files should maintain comments, order, and whitespace
- Use Jinja2 templating syntax compatible with Ansible

## Testing Guidelines

- Tests are located in the `tests/` directory
- Test files follow the pattern `test_*.py`
- Use pytest fixtures from `tests/utils.py` and `tests/fixtures/`
- Integration tests may require external tools (Terraform, Helm, kubectl)
- Set `UNFURL_NORUNTIME=1` for tests that don't need runtime environment

## Environment Variables

Key environment variables for testing:
- `UNFURL_HOME` - Unfurl home directory
- `UNFURL_TMPDIR` - Temporary directory for tests
- `UNFURL_NORUNTIME=1` - Skip runtime environment setup
- `UNFURL_LOGGING` - Logging level (debug, info, warning, error)

## Git Configuration

Before running tests, configure git:
```bash
git config --global user.email "test@example.com"
git config --global user.name "Test User"
git config --global init.defaultBranch main
git config --global protocol.file.allow always
```

## Important Notes

- The `tosca-parser` directory is a git submodule - use `git submodule update --init --recursive` if needed
- The Rust extension must be built before running Python code that uses the solver
- Some tests require external services (Kubernetes, Docker) and may be skipped in CI
