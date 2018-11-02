# GitErOp

GitErOp is a tool for saving hetrogenous devops tasks and deployment history in a git repository so they that can be shared and automatically redeployed.

The initial application for building and maintaining fully-reproducible Kubernetes and Openshift clusters but nothing about its functionality is specific to those technologies.

# Basic functionality and concepts

GitErOp relies on other tools and services to actually do the work, they are encapulated in a "Configurator" interface, which tries to impose the minimum requirements and semantics.

The manifest defines arbitrary and abstract representations of resources using locally defined identifiers. The goal is to make it easy to build humane specifications.

Git repository that contains YAML files that specifies how resources should be configured.

Running GitErOp attempts to apply that specification and commits the results of that run. The exact versions of all external assets and tools utilized are recorded so playback can be fully reproducible.

# Installation

## Requirements

Either python 2.7 or 3.6, ansible, git, openshift client tools (`oc`)

## From source

## Container Image

# Developing

Clone github.com/onecommons/giterop.

## Running unit tests

You can use `tox` to run the unit tests inside the supported python environments with the latest source installed.
Install tox `pip install tox` and then run `tox` in source root. To install the dependencies you may need header files installed by the following OS packages: `python-dev`, `libcrypt-dev`, `openssl-dev`. (Note: if installation of a dependency fails, reinvoke `tox` with `-r` to recreate the test environment.)

Arguments after `--` are passed to the test runner, e.g. to run an individual test: `tox -- -p test_attributes.py`.

# Getting Started

 1. create a repo
 2. configure giterop
     1. kms setup: add credentials
     2. create manifest with root resource: configure using kms
     3. (optional) run bootstrap to create a secure root resource
 3. start creating your manifest

# Usage

add / remove resource

## Existing cluster

1. create a manifest file defining your clusters
2. use giterop instead of `kubectl`

## Build a new cluster

## Updating a cluster

Adding a component
