===============
Glossary
===============

Terminology from TOSCA
=======================

Service Template
++++++++++++++++

  Services templates specify how resources should be configured using the `TOSCA` standard. Templates can live in their own git repositories.

Node Template
+++++++++++++

  A Node Template specifies the occurrence of a component node as part of a :ref:`Topology<topology>` Template. Each Node Template refers to a Node Type that defines the semantics of the node (e.g., properties, attributes, requirements, capabilities, interfaces). Node Types are defined separately for reuse purposes.

Entity Types
++++++++++++

  Every entity in TOSCA (including Nodes, Relationships, Artifacts and Data) has a declared type and custom type hierarchies can be defined in the `Service Template`.

Instance
++++++++

  The running instance of a `Node template`, typically instantiated when running the deployment workflow.

Properties
++++++++++

  Name values that are declared in the specification of the node and relationship templates.

Attributes
++++++++++

  Attribute refer to the actual values of an instance. They may be a reflect declared properties' values or they may be determined at runtime.

.. _topology:

Topology
++++++++

  The set of Node Template and Relationship Template definitions that together define the topology model of a service as a (not necessarily connected) directed graph.

Relationships and Requirements
++++++++++++++++++++++++++++++

  Relationships between nodes are specified in the *requirements* section of a *node template*.

Relationship Template
+++++++++++++++++++++

  Specifies the occurrence of a relationship between nodes in a Topology Template. Each Relationship Template refers to a Relationship Type that defines the semantics relationship (e.g., properties, attributes, interfaces, etc.). Relationship Types are defined separately for reuse purposes.

Capabilities
++++++++++++

  Nodes can specify the types of capabilities they may have and the relationships between nodes can target specific capabilities.

.. _operation:

Operation
+++++++++

  The basic unit of work invoked by an TOSCA orchestrator such as Unfurl. Interfaces associated with TOSCA types define a collections of operations and Node and Relationship templates specify their implementation.

Workflow
++++++++

  A plan for executing operations on instances. Predefined workflows include *deploy* for instantiating a topology, *undeploy* for destroying it, *discover* for discovering existing instances, and *check* for checking the status of existing instances.
  Custom workflows can be defined in a service template.

Unfurl specific
===============

Ensembles
+++++++++

  A representation of an isolated collections of resources. Ensembles are implemented in manifest files that describe the resources' operations, properties and current state. Ensembles can lives in its own git repository, maintaining a history of changes applied to its resources.

Configurators
+++++++++++++

  A software plugin that implements an operation. Configurators apply changes to instances. There are built-in configurators for shell scripts, Ansible playbooks, Terraform configurations, and Kubernetes resources or you can include your as part of the service specification.

Installations
+++++++++++++

  Installation can create and update many instances as a side effect of its operation and only the important ones need to be reified. See Helm Release as an example installation.

Installers
++++++++++

  A node template represents the software that creates and manages installations. Installers allow implementations to be reified as first-class instances so they can be instantiated and configured themselves, enabling the local client environment to be bootstrapped as well.

Eval expressions
++++++++++++++++

  YAML markup that is evaluated when running a job.

Job
+++

  A jobs executes a *workflow* by instantiating a plan of tasks to run.

.. _tasks:

Tasks
+++++

  Instantiates a configurator to carry out the given *operation*.

Config Change
+++++++++++++

  A persistent record of the changes made by a task.

Changelog
+++++++++

  A YAML file that describes job run with the changes made to the ensemble.

Secrets
+++++++

  An object that represents a secret value. Secrets are stored in a separate configuration file outside of version control or retrieved from a KMS such as Hashicorp Vault. Sensitive values and objects tainted by sensitive values are always redacted when serialized or logged.

Local values
++++++++++++

  Values and configurations settings that are dependent on the local environment and therefore should be saved separately from a shared repository or deployment history. For example, connection or proxy settings. Marking which settings are local helps enable a reproducible infrastructure.

External values
+++++++++++++++

  Values that are not directly serialized into the model but rather referenced by a name that is resolved during task execution. Examples are secrets, local values, file references, and external (imported) instances. External values can indicate if they've changed and so can participate in incremental updates of the ensemble.

External instances
++++++++++++++++++

  External instances are instances that are part of a separate ensemble. They are only accessible through the `external` expression function unless the service template has corresponding node template with a ``select`` or ``substitute`` directive.

.. Dependencies
..  Tracking dependencies between instances enable the incremental update of ensembles. Dependencies can be inferred through the relationships defined in TOSCA service template, by the expressions that define an instance's properties and attributes, or dynamically by configurators using the Task API.
