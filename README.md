# GitErOp

GitErOp is a tool for saving hetrogenous devops tasks and deployment history in a git repository so they that can be shared and automatically redeployed.

The initial application for building and maintaining fully-reproducible Kubernetes and Openshift clusters but nothing about its functionality is specific to those technologies.

# Basic functionality and concepts

GitErOp relies on other tools and services to actually do the work, they are encapulated in a "Configurator" interface, which tries to impose the minimum requirements and semantics.

The manifest defines arbitrary and abstract representations of resources using locally defined identifiers. The goal is to make it easy to build humane specification.

Git repository that contains YAML files that specifies how resources should be configured.

Running GitErOp attempts to apply that specification and commits the results of that run. The exact versions of all external assets and tools utilized are recorded so playback can be fully reproducible.

# Installation

## Requirements

Python, ansible, git, openshift client tools (`oc`)

## From source

## Container Image

# Usage

## Existing cluster

1. create a manifest file defining your clusters
2. use giterop instead of `kubectl`

## Build a new cluster

## Updating a cluster

Adding a component
