TOSCA
=====

Introduction
~~~~~~~~~~~~

Topology and Orchestration Specification for Cloud Applications also abbreviated as TOSCA is an OASIS open standard that defines the interoperable description of services and applications hosted on the cloud and elsewhere. This includes their components, relationships, dependencies, requirements, and capabilities, thereby enabling portability and automated management across cloud providers regardless of underlying platform or infrastructure. 


.. seealso:: For more information, please refer to the `TOSCA Documentation. <https://www.oasis-open.org/standard/tosca/>`_



Using TOSCA with Unfurl
~~~~~~~~~~~~~~~~~~~~~~~~

TOSCA supports the automatic matching of cloud service provider capabilities, with application/service requirements, thus enabling a competitive ecosystem. Unfurl makes use of service templates defined using OASIS's `TOSCA <https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca>`_ simple profile in YAML version 1.3 format. These service templates are used with the local unfurl configuration YAML file and secrets to form an Ensemble which then stores all the essential information regarding the model of cloud resources that it manages from the TOSCA spec apart from other things it records.

.. image:: images/tosca_unfurl.png
   :width: 100px
   

.. seealso:: For more information, please refer to the `HTML <https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/TOSCA-Simple-Profile-YAML-v1.3.html>`_ and `PDF <https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/TOSCA-Simple-Profile-YAML-v1.3.pdf>`_ version of the TOSCA Simple Profile YAML documentation.

Authoring TOSCA Templates
~~~~~~~~~~~~~~~~~~~~~~~~~~

In order to work with the TOSCA templates, it is important to familiarize yourself with various options of the topology blueprint that can be put together in a template. In this section,we will go through all the essential components.

Version
^^^^^^^

``tosca_definitions_version`` is a top level property of the blueprint which is used to specify the DSL version used.

.. code:: 
 
 tosca_definitions_version: tosca_simple_unfurl_1_0_0

The other tosca supported unfurl definition includes:

* tosca_simple_unfurls_1_0_0

.. note:: TOSCA definition version needs to be declared at the start of the main file.

Metadata
^^^^^^^^

Metadata includes the templates details such as template name, author and version.


.. code::

 metadata:
   template_name: Unfurl types
   template_author: onecommons.org
   template_version: 1.0.0

A list of unfurl supported template names are as follows:

* Unfurl types

Node Types
^^^^^^^^^^

``node_types`` are used to define reusable properties, interfaces and capabilities for each node. These definitions can be inherited for further use.

Schema
^^^^^^

The table below walks you through the various options available in the schema.

.. list-table:: 
   :widths: 25 25 50
   :header-rows: 1

   * - Parameter
     - Type
     - Description
   * - ``derived_from``
     - String
     - Used to reuse a common node template or build on top of the existing one.
   * - ``interfaces``
     - Definition
     - Existing interfaces available for use such as ``Install``, ``Standard``, ``defaults``, etc
   * - ``properties``
     - Definition
     - Refers to the properties that a node can adopt. The common things to be defined as a property include a description, type, default and required.
   * - ``capabilities``
     - Definition
     - Refers to the capability associated with a node such as ``installer``, ``host``, ``endpoint``, etc.   
   * - ``requirements``
     - Definition
     - Refers to the node requirements such as ``installer``. 

Example
^^^^^^^

The example below shows the definition of node types used with Unfurl.


.. code::

 node_types:
   tosca.nodes.Root:
     interfaces:
       Install: # all nodes can implement this interface
         type: unfurl.interfaces.Install

   unfurl.nodes.Installer:
     derived_from: tosca.nodes.Root
     capabilities:
       installer:
         type: unfurl.capabilities.Installer

   unfurl.nodes.Installation:
     derived_from: tosca.nodes.Root
     requirements:
       - installer:
           capability: unfurl.capabilities.Installer
           node: unfurl.nodes.Installer
           relationship: unfurl.relationships.InstalledBy
           occurrences: [0, 1] # it isn't necessarily required

   unfurl.nodes.Default:
     derived_from: unfurl.nodes.Installation
     description: "Used if pre-existing instances are declared with no TOSCA template"

   unfurl.nodes.Installer.Terraform:
     derived_from: unfurl.nodes.Installer
     properties:
       dir:
         type: string
         default:
           eval:
             get_dir: spec.home
     interfaces:
       defaults:
         implementation:
           className: unfurl.configurators.terraform.TerraformConfigurator
         inputs:
           dir: { get_property: [SELF, dir] }
       Standard:
         operations:
           delete:
           create:
       Install:
         operations:
           check:


Description
^^^^^^^^^^^

* As we can see ``unfurl.nodes.Installer`` derives the interfaces from ``tosca.nodes.Root`` which was defined in the definition above it. ``tosca.nodes.Root`` has also been derived in the subsequent examples.
* ``unfurl.nodes.Installer.Terraform`` makes use of the ``defaults``, ``Standard`` and ``Install`` interfaces.


Extensions
~~~~~~~~~~

* add 'any' schema type for properties and attributes definitions
* 'additionalProperties' field in type metadata
* allow metadata field on inputs, outputs, artifacts, and repositories
* add "sensitive" property and datatype metadata field
* add "immutable" property metadata field
* add "environment" keyword to implementation definition
* add "eval" function
* add "type" in capability assignment
* allow workflow to be imported
* workflow "target" accepts type names
* groups can have other groups as members
* An operation's ``operation_host`` field can also be set to a node template's name.
* added ``OPERATION_HOST`` as a reserved function keyword.
* add "discover" and "default" directives
* add "default_for" keyword to relationship templates
* add "defaults" section to interface definitions

Not yet implemented and non-conformance with the TOSCA 1.3 specification
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* The ``get_operation_output`` function (use `resultTemplate` instead)
* "copy" keyword (use the ``dsl`` section or `merge directives` instead)
* `get_artifact` function (only implemented for artifacts that are container images)
* CSAR manifests and archives (implemented but untested)
* substitution mapping
* triggers
* notifications
* node_filters
* xml schema constraints

Extensions to built-in definitions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. include:: tosca-ext.yaml
   :code: YAML
