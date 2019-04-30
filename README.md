## What is GitErOp?

At its most ambitious, GitErOp aims to be a package manager for the cloud, enabling you to easily deploy and integrate live services.

More simply, it is a tool that works with Git to record and deploy changes to your DevOps infrastructure.

## Features

* **Next-level GitOps**: both configuration and operational status stored in git
* **Hermetic**: tracks exact version of environment and deployment artifacts
* **Reproducible**: hermetic builds + git + locked-down, immutable infrastructure = full reproducibility
* **Incremental**: only applies necessary changes
* **Fast**: the above three features combined enable lightening-fast updates
* **Configuration tool agnostic** with built-in support for Ansible and Terraform
* **Secrets**: keeps secrets out of git so repos can be safely made public
* **Flexible workflow**: use both as development tool on client or automated production deployment on server
* **Dependency management**: Easily track dependencies and changes across infrastructure layers and boundaries.
* **Zero installation clients**: Use client-side container support to avoid client-side installation requirements.

## Installation

### Requirements

Python (2.7, 3.5, or 3.6); git; docker 13 or later

### From source

## Developing

Clone https://github.com/onecommons/giterop

To build documentation: Run `tox -e docs`.

### Running unit tests

You can use `tox` to run the unit tests inside the supported python environments with the latest source installed.
Install tox `pip install tox` and then run `tox` in source root. To install the dependencies you may need header files installed by the following OS packages: `python-dev`, `libcrypt-dev`, `openssl-dev`. (Note: if installation of a dependency fails, reinvoke `tox` with `-r` to recreate the test environment.)

Arguments after `--` are passed to the test runner, e.g. to run an individual test: `tox -- -p test_runtime.py`.

## Getting Started

 1. create a repo
 2. configure giterop
     1. kms setup: add credentials
     2. create manifest with root resource: configure using kms
     3. (optional) run bootstrap to create a secure root resource
 3. start creating your manifest

## Usage
