===============
Getting Started
===============

This tutorial will walk you through setting up a local environment that provides access to
the resources you want to manage with Unfurl, such as cloud provider credentials.

It will then guide you through creating your first Unfurl repository with a sample deployment you can start using immediately: an online Unfurl lock. An Unfurl lock is an online resource Unfurl can use to safe guard against
multiple instances of Unfurl modifying the same resources at the same time.

We'll see how we can use a service template that is cloud-agnostic and, once it is deployed, how to import and use that live instance in our own manifests.

(The lock manifest gets added in our home config so all our manifests automatically utilize it.)

 1. create a repo
 2. configure unfurl
     1. kms setup: add credentials
     2. create manifest with root resource: configure using kms
     3. (optional) run bootstrap to create a secure root resource
 3. start creating your manifest

Setting up your local environment
=================================

You configure how to connect to online cloud providers and services by creating
`relationship templates` in the local ensemble, which is TOSCA's way of specifying how instances connect.

Cloud provider instances are identified by context independent attributes such as an account id (usually automatically detected) as opposed to client-dependent connection attributes such as an API key. But including cloud provider instances in your specification is optional if the connection info is just passed through to external tools.


1. Initial set up of home environment

The unfurl home directory contains configuration settings you want shared by all your projects on that device as a local manifest that represents the device. By updating and deploying that local manifest you can discover and configure local settings and resources (such as cloud provider accounts or client software) that your Unfurl projects can utilize.

1. run `unfurl init`
2. take a look at `~/unfurl_home/local/manifest.yaml`, you see something like:

.. code-block:: YAML

  node_templates:
     aws:
     gcp:
     k8s:
     docker:
     ansible:

When you run `unfurl discover`, the job will examine you system for configuration information and add what it finds to the manifest. Before running that command, you can comment out or delete the templates that you want to skip.

After running `unfurl discover` successfully, your manifest will look something like:


Creating a manifest to represent your local system enables to unfurl to configure your local environment.

.. code-block:: YAML

  unfurl: 0.1

* A private, local repository and a secret store.

Add local resources and credentials
