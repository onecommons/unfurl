# Overview

Unfurl is a tool that works with Git to record and deploy changes to your DevOps infrastructure. 
It tracks configuration changes, keeping a history of exactly how you did it and what the results are, so you can easily repair or recreate what you did later. 

Unfurl integrates with the deployment tools you are already using, like Ansible, Terraform and Helm, organizing their usage into shareable abstractions that ease migrations to new environments as well as share and reuse your work.

## Goals

* **Next-level GitOps**: both configuration and operational status stored in git
* **Hermetic**: tracks exact version of environment and deployment artifacts
* **Reproducible**: hermetic builds + git + locked-down, immutable infrastructure = full reproducibility
* **Incremental**: only applies necessary changes
* **Fast**: the above three features combined enable lightening-fast updates
* **Configuration tool agnostic** and includes built-in support for Ansible and Terraform
* **Secrets**: key manager integration; keeps secrets out of git so repos can be safely made public
* **No server, no agent**: simple, stand-alone CLI that can be used both as development tool on client or for automated production deployment on a server
* **Dependency management**: Easily track dependencies and changes across infrastructure layers and boundaries. 
* **Zero installation**: Uses client-side container support to bootstrap and automate installation requirements.

## Features

* Specifications, instance status, and change history authored and recorded in a simple YAML vocabulary. 
* Or use [TOSCA's](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) (Topology and Orchestration Specification for Cloud Applications) YAML vocabulary for more carefully typed specifications.
* Editor friendly config files: 
  - Comments, order, and whitespace are preserved.
  - Syntactic macros for YAML provide generic facility for re-use and avoiding verbose, boiler-plate
* Path-based query DSL to express dynamic relationships between resources and configurations
* Ansible-compatible Jinja2 templates
* Records history of changes and commits them to a Git repository.
* API for dynamic configuration allows scripted specifications to be recorded alongside declarative ones.

# Installation

`pip install unfurl`

### Requirements

Python (2.7, 3.5, or 3.7); git

# Developing

Clone https://github.com/onecommons/unfurl

To build documentation: Run `tox -e docs`.

To build a distribution package run:

`python setup.py sdist bdist_wheel`

You can now install this package with pip, for example:

`pip install ../dist/unfurl-0.0.1.dev183-py2.py3-none-any.whl`

### Running unit tests

You can use `tox` to run the unit tests inside the supported python environments with the latest source installed.
Install tox `pip install tox` and then run `tox` in source root. To install the dependencies you may need header files installed by the following OS packages: `python-dev`, `libcrypt-dev`, `openssl-dev`. (Note: if installation of a dependency fails, reinvoke `tox` with `-r` to recreate the test environment.)

Arguments after `--` are passed to the test runner, e.g. to run an individual test: `tox -- -p test_runtime.py`.

## Learn more

Check out the rest of Unfurl's documentation [here](https://www.onecommons.org/unfurl/guide.html) 
