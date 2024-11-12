===============
Glossary
===============

Terminology from TOSCA
=======================

Service Template
++++++++++++++++

Services templates specify how resources should be configured using the `TOSCA` standard. 

Node Template
+++++++++++++

A Node Template defines a node that gets instantiated into an instance (or resource) when the service template is deployed.  A node template uses its node type as a prototype and can define properties, capabilities, artifacts, and requirements specific to the template.

Entity Types
++++++++++++

Every entity in TOSCA has a declared type (including node types, relationship types, Artifact types, and data types) and type inheritance hierarchies can be defined in the `Service Template`.

Instance
++++++++

The running instance of a `Node template`, typically instantiated by the deployment workflow.

Properties
++++++++++

Name values that are declared in the specification of the node and relationship templates.

Attributes
++++++++++

Attribute refer to the actual values of an instance. They may be a reflect declared properties' values or they may be determined at runtime.

Topology
++++++++

The set of node templates and other components along with their relationships and dependencies and that together define the model of the service being deployed.

Artifact
++++++++

A representation of assets such as files, container images, or software packages that need to be deployed or used by an implementation of an operation.

Repository
++++++++++

A TOSCA abstraction that can represent any collection of artifacts. For example, a repository can be git repository, an OCI (Docker) container registry, or a local file directory

Requirement
+++++++++++

A requirement is description of a dependency between two node templates.

Relationship Template
+++++++++++++++++++++

A relationship template is used to provide additional information about a requirement, for example how to establish a connection between two nodes.

Capabilities
++++++++++++

Nodes can specify the types of capabilities they may have and requirements can target specific capabilities.

Interface
++++++++++

A collections of `operations<Interfaces and Operations>` associated with a node template or other TOSCA types.

Operation
+++++++++

A definition of an invocation of an artifact or a configurator. A TOSCA orchestrator like Unfurl instantiates a service template by executing operations.

Workflow
++++++++

A plan for executing operations on instances. `Predefined workflows<Jobs and Workflows>` include *deploy* for instantiating a topology, *undeploy* for destroying it, *discover* for discovering existing instances, and *check* for checking the status of existing instances. Custom workflows can be defined in a service template.

Unfurl specific
===============

Ensembles
+++++++++

  A representation of an isolated collections of resources. Ensembles are implemented in manifest files that describe the resources' operations, properties and current state. Ensembles can lives in its own git repository, maintaining a history of changes applied to its resources.

.. _configurator_def:

Configurators
+++++++++++++

  A software plugin that implements an operation. Configurators apply changes to instances. There are built-in configurators for shell scripts, Ansible playbooks, Terraform configurations, and Kubernetes resources or you can include your as part of the service specification.

Installations
+++++++++++++

  Installation can create and update many instances as a side effect of its operation and only the important ones need to be reified. See Helm Release as an example installation.

Installers
++++++++++

  A node template represents the software that creates and manages installations. Installers allow implementations to be reified as first-class instances so they can be instantiated and configured themselves, enabling the local client environment to be bootstrapped as well.

Deployment blueprint
++++++++++++++++++++

A deployment blueprint is a blueprint that is only applied when its criteria matches the deployment environment. It inherits from a topology template and includes node templates that override the topology template's.

Eval expressions
++++++++++++++++

  YAML markup that is evaluated when running a job.

Job
+++

  A jobs executes a `workflow` by instantiating a plan of tasks to run.

.. _tasks:

Tasks
+++++

  Instantiates a `configurator` to carry out the given :std:ref:`operation`.

Config Change
+++++++++++++

  A persistent record of the changes made by a `task<tasks>`.

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
