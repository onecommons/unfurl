.. _tosca:


TOSCA
=====

.. contents::
   :local:

Introduction
^^^^^^^^^^^^

The Topology and Orchestration Specification for Cloud Applications (TOSCA) is an `OASIS open standard <https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca>`_ that provides language to describe a topology of cloud based web services, their components, relationships, and the processes that manage them. TOSCA provides mechanisms for abstraction and composition, thereby enabling portability and automated management across cloud providers regardless of underlying platform or infrastructure. 
 
The TOSCA specification allows the user to define a `service template` which describes an online application or service and how to deploy it.

Core TOSCA Concepts
^^^^^^^^^^^^^^^^^^^

The figure below illustrates the core components of a TOSCA service template.
At the heart of a TOSCA service template is a `topology template` that describes a model of the service's online resources, including how they connect together and how they should be deployed. These resources are represented by a `node template`. 
The Node Template and a Relationship Template are the building blocks of any TOSCA Specification. 

.. https://app.diagrams.net/#G1rbe28yAmiULdCV2mtNJ_b0AWJFiG0iVi

.. image:: images/service_template.svg
   :width: 90%
   :align: center


* *Node templates* describe the resources that will be instantiated when the service template is deployed. They are linked to other nodes through relationships.
* *Relationship templates* can be used to provide additional information about those relationships, for example how to establish a connection between two nodes.
* *Interfaces* are a collections of user-defined *operations* that are invoked by a TOSCA orchestrator like Unfurl. TOSCA defines a standard interface for lifecycle management operations (creating, starting, stopping and destroying resources) and the user can define additional interfaces for "Day Two" operations, such as maintenance tasks.
* *Artifacts* such as container images, software packages, or files that need to be deployed or used as an implementation for an operation. 
* *Workflows* allows you to define a set of manually defined operations to run in a sequential order.
* *Type definitions* TOSCA provides an object-oriented type system that lets you declare types for all of the above components as well as custom data types that provide validation for properties and parameters.

.. seealso:: For more information, see the full `TOSCA Language Reference` and the :ref:`Glossary<glossary>` section.

Service Template
^^^^^^^^^^^^^^^^^

A TOSCA service template contains all the information needed to deploy the service it describes. In Unfurl, a service template can be a stand-alone YAML file that is included in the `ensemble.yaml` configuration file or embedded directly in that file as a child of the :tosca_spec:`Service templates<DEFN_ELEMENT_SERVICE_TEMPLATE>` element.

A service template has the following sections:

* :doc:`Metadata <toscaref/spec-tosca_def_version>` sections, which includes the ``tosca_definitions_version``, ``description``, ``metadata``, ``dsl_definitions``
* `imports` and `repositories` sections 
* Types sections that contain types of Node, Relationships, Capabilities, Artifacts, Interfaces, Policy and Groups
* Topology Template which include sections for :std:ref:`Inputs`, `outputs`, Node and relationship templates, :ref:`substitution_mappings`, `groups`, :ref:`policies<policy>` and `workflows`.

Example
-------

.. tab-set-code::

  .. literalinclude:: ./examples/tosca-outline.yaml
    :language: yaml

  .. literalinclude:: ./examples/tosca-outline.py
    :language: python

Types
^^^^^

Every entity in TOSCA (including Nodes, Relationships, Artifacts and Data) has a declared type. custom type hierarchies can be defined in the `Service Template`.
Types declare the required properties, default definitions, and interface operations for an entity. Each type of entity has can have its own section in the service template, for example, ``node_types``, ``relationship_types``, ``data_types``, ``artifact_types``, ``interface_types``, etc. but type names all are share one namespace in the YAML document or Python module they are defined in.

Example
-------

This example defines a node type named "MyApplication" that inherits from the "tosca.nodes.SoftwareComponent" node type.

.. tab-set-code::

  .. literalinclude:: ./examples/tosca-type-example.yaml
    :language: yaml

  .. literalinclude:: ./examples/tosca-type-example.py
    :language: python

Topology Template
^^^^^^^^^^^^^^^^^^

The topology Template defines the components of the service being deployed. It can be thought of as a graph of node templates and other components along with their relationships and dependencies.

Topologies can parameterized with `inputs <Topology Inputs>`, define `outputs<topology_outputs>`, and contains `node templates`, `relationship templates`, `groups`, `policies<policy>`, and `workflows`.

Topologies can be embedded in other topologies via `substitution_mappings`.

Example
-------

.. tab-set-code::

  .. literalinclude:: ./examples/topology-template.yaml
    :language: yaml

  .. literalinclude:: ./examples/topology-template.py
    :language: python

Deployment blueprints
^^^^^^^^^^^^^^^^^^^^^

A deployment blueprint is an Unfurl extension that allows you to define blueprint that is only applied when its criteria matches the deployment environment. It inherits from the main topology template and node templates with matching names replace the ones in the topology template's. For example, see this `example<deployment_blueprint_example>`.

Node Template
^^^^^^^^^^^^^

A Node Template defines a node that gets instantiated into an instance (or resource) when the service template is deployed.  A node template uses its node type as a prototype and can define properties, capabilities, artifacts, and requirements specific to the template.

Example
-------

.. tab-set-code::

  .. literalinclude:: ./examples/tosca-node-template.yaml
    :language: yaml

  .. literalinclude:: ./examples/tosca-node-template.py
    :language: python


Requirements and Relationships
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Requirements let you define relationships between nodes and must be assigned a relationship type. Requirements are declared as a list on the node template, allowing multiple requirements with the same name, whose number is constrained by the :tosca_spec:`occurrences<_Toc50125352>` keyword in the requirement's :tosca_spec:`definition<_Toc50125352>`.

Relationship types can have properties and have interfaces and operations associated with them. For example, the orchestrator will use operations defined for the built-in :tosca_spec:`Configure<_Toc50125679>` interface to create or remove the relationship.

Requirements :tosca_spec:`declared on a node type<_Toc50125352>` define the constraints for that requirement, for example the type of node it can target or the number of target matches each node template can have for that requirement.

Requirements :tosca_spec:`declared on a node template<_Toc50125406>` define the node that the requirement is targeting, either directly by naming that node template, or by defining the search criteria for finding the match, for example by including a ``node_filter``. 

So when the ``node`` field refers to an node type in requirement defined on a node template its defining a search criteria for finding a matching node template, but when the requirement is on node type, it is only defining a validation constraint where the node template's match must implement that type -- it won't use that field to search for a match.

Relationship templates can be declared to provide specific information about the requirement's relationship with its target node.  Relationship templates can be defined directly inline in the requirement or they can be declared separately in the ``relationship_templates`` section of the topology and referenced by name.

Examples
--------

This simple example using built-in TOSCA types that defines the "host" requirement as using the ``tosca.relationships.hostedOn`` relationship, so we can define the requirement with just the name of the target node template:

.. code:: yaml

  topology_template:
    node_templates:
      myApp:
        type: tosca.nodes.SoftwareComponent
        requirements:
          - host: another_node


The following more complex example first defines a "MyApplication" node type that requires one "db" relationship of type "DatabaseConnection".  Then it defines a node template ("myApp") with that node type with a "db" requirement that uses a node filter to find the matching target node and a named relationship template of type "DatabaseConnection" with properties for connecting to the database. 

.. tab-set-code::

  .. literalinclude:: ./examples/tosca-requirements.yaml
    :language: yaml

  .. literalinclude:: ./examples/tosca-requirements.py
    :language: python

.. _tosca_artifacts:

Artifacts
^^^^^^^^^

An artifact is an entity that represents an asset such as files, container images, or software packages that need to be deployed or used by an implementation of an operation. Like other TOSCA entities, artifacts have a type and can have properties, attributes, and operations associated with it.

Artifacts are declared in the ``artifacts`` section of node templates and node types, and can refer to the `repository<repositories>` that it is part of.

This example defines an artifact that is a container image, along with a `repository<repositories>` that represents the image registry that manages it:

.. tab-set-code::

  .. literalinclude:: ./examples/artifact1.yaml
    :language: yaml

  .. literalinclude:: ./examples/artifact1.py
    :language: python

Artifacts can be used in the following ways:

* An operation's `implementation's<implementation>`, ``primary`` field can be assigned an artifact which will be used to execute the operation. Artifacts derived from ``unfurl.artifacts.HasConfigurator`` will use configurator set on its type, otherwise it will treated as a shell script (using the `Cmd` configurator) unless the ``className`` field is set in the implementation.
* An implementation can also list artifacts in the ``dependencies`` field which will be installed if necessary.
* The `get_artifact` TOSCA function to reference to artifact's URL or local location (if available).
* An artifact and its properties can be accessed in `Eval Expressions` via the `.artifacts<Special keys>` key. (see `Artifact enhancements`) or as :py:class:`Node` attributes when using the `Python DSL`. 

Artifacts that are referenced in an operation's implementation will be installed on the operation's :tosca_spec:`operation_host<_Toc50125294>` (by default, where Unfurl is running) as part of the `Job Lifecycle` if the artifact has ``Standard`` operations (``create`` or ``configure``) defined for it.

.. seealso:: For more information, see the :ref:`configurators-artifacts` chapter and the :tosca_spec:`TOSCA Artifact Specification <_Toc50125252>`

Interfaces and Operations
^^^^^^^^^^^^^^^^^^^^^^^^^

An :tosca_spec:`Operations<DEFN_ELEMENT_OPERATION_DEF>` defines an invocation on an artifact or `configurator`. A TOSCA orchestrator like Unfurl instantiates a service template by executing operations. An operation has an `implementation`, `inputs<operation_inputs>`, and `outputs<operation_outputs>`. 

Conceptually an operation is comparable to method in an object-oriented language where the implementation is the object upon which the method is invoked, the inputs are the method's arguments, and its outputs are the method's return values.

An :tosca_spec:`Interface<_Toc50125307>` is a collections of :std:ref:`operations<operation>` that can be defined in the ``interfaces`` section on TOSCA types or directly on TOSCA templates for nodes, relationships, and artifacts.

Example
-------

.. tab-set-code::

  .. literalinclude:: ./examples/tosca-interfaces.yaml
    :language: yaml

  .. literalinclude:: ./examples/tosca-interfaces.py
    :language: python

Operations can be invoked in the following ways:

* The built-in :tosca_spec:`Standard<_Toc50125674>` and :tosca_spec:`Configure<_Toc50125679>` interfaces are invoked during deploy and undeploy workflows
* Unfurl's built-in Install are invoked for check and discovery.
* Custom operations can be invoked with `run<Ad-hoc Jobs>` workflow.
* Operations can be invoked directly by custom `workflows`, the `delegate` configurator, or using Unfurl's Python `apis<api>`.

Implementation
---------------

As the example above illustrates, an operation's :tosca_spec:`implementation<_Toc50125293>` field describes which artifacts or :std:ref:`configurator<configurators>` (Unfurl's plugin system for implementing operations) to use to execute the operation.

As an Unfurl extension, an implementation section can also include an `environment` key with `Environment Variables` directives and a ``className`` field to explicitly name the configurator.

.. _operation_inputs:

Inputs
-------

Inputs are passed to the implementation of the operation.
A TOSCA type can (optionally) define the expected inputs on a operation in much like a property definition.
Unlike properties on templates, you can pass inputs to an operation that are not defined in its inputs definition.
In Unfurl, inputs are evaluated lazily as they are accessed by the operation's configurator.

Default inputs can defined directly on the interface and will be made available to all operations in that interface.
If inputs are defined on multiple types in a type hierarchy for the same operation, they are merged together along with any inputs for that operation defined on directly on the template.
Note: it is not type-safe to remove arguments from the signature of an overloaded method, doing so will report a static type error.
So TOSCA YAML's behavior of merging inherited inputs is a no-op and therefore consistent with type-safe Python DSL usage.

For example:

.. tab-set-code::

  .. literalinclude:: ./examples/tosca_inputs.yaml
    :language: yaml

  .. literalinclude:: ./examples/tosca_inputs.py
    :language: python


.. _operation_outputs:

Outputs
-------

An operation can define an :tosca_spec:`attribute mapping<_Toc50125291>` that specifies how to apply the operation's outputs. The meaning of keys in the mapping depends on the operation's configurator, for example, a Ansible fact or a Terraform output.
If mapping's value is a string, it names the attribute on the instance where the output will be saved.
If the value is null, no attribute mapping will be made but the output will be available to the `resultTemplate` and saved in the ensemble.

Interface types
---------------

Interface types define names of the operations in an interface along with their inputs and outputs. TOSCA defines a `built-in interface types<built-in interfaces>` for lifecycle management operations and additional interface types can be declared in the service template for "Day Two" operations, such as maintenance tasks.

For example, Unfurl adds a built-in interface type for discovering resources, which it defines as:

.. _install_interface:

.. code-block:: yaml

  interface_types:
    unfurl.interfaces.Install:
      derived_from: tosca.interfaces.Root
      operations:
        check:
          description: Checks and sets the status and attributes of the instance
        discover:
          description: Discovers current state of the current instance and (possibly) related instances, updates the spec as needed.
        revert:
          description: Restore the instance to the state it was original found in.
        connect:
          description: Connect to a pre-existing resource.
        restart:
          description: Restart the resource.

Built-in interfaces
-------------------

TOSCA defines two built-in interface types that are invoked to deploy a topology: the :tosca_spec:`Standard<#_Toc50125669>` interface for node templates and :tosca_spec:`Configure<_Toc50125679>` interface for relationships templates.

The former defines lifecycle management operations (creating, starting, stopping and destroying resources) that are invoked when creating and deleting node instances and latter for configuring the `requirements` between nodes.

It is not a requirement to define every operation for every template; the operation will be silently skipped if not defined. With the Standard interface, only the ``configure`` operation needs to be defined to instantiate a node, if the ``create`` operation isn't defined, Unfurl will assume that ``configure`` will both create and configure the resource (and reconfigure when updating an existing deployment).

In addition, Unfurl provides a built-in `Install<install_interface>` interface which is invoked when running `check<Checking resources>` and `discover<Resource Discovery>` workflows.

Complete Example
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Combining the above examples into one file, we have a complete service template:

.. tab-set-code::

  .. literalinclude:: ./examples/service-template.yaml
    :language: yaml

  .. literalinclude:: ./examples/service_template.py
    :language: python
