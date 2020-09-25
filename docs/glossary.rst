===============
Glossary
===============

### From the TOSCA Specification:

Service Template: Services templates specify how resources should be configured using the [TOSCA standard](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca). Templates can live in their own git repos

Node and node templates: Abstract or concrete

Types

Instances

Properties

Attributes

Topology

Relationships and Requirements

Capabilities

Operations

Workflows: The weaving process generates the workflow

### Unfurl specific

Ensembles: Resource manifests describe the current state of resources and maintain a history of changes applied to those resources. Each manifest lives in its own git repo, which corresponds to the lifespan of the resources represented in the manifest.

Configurators: A software plugin that implements an operation. Configurators apply changes to instances. There are built-in configurators for shell scripts, Ansible playbooks, Terraform configurations, and Kubernetes resources or you can include your as part of the service specification.

Installations: Installation can create and update many instances as a side effect of its operation and only the important ones need to be reified. See Helm Release as an example installation.

Installers: A node template represents the software that creates and manages installations. Installers allow implementations to be reified as first-class instances so they can be instantiated and configured themselves, enabling the local client environment to be bootstrapped as well.

Ref expressions:

Job: A jobs executes a workflow by instantiating a plan of tasks to run.

Tasks: Instantiates a configurator to carry out the given `operation`. It's results are

ConfigChange: a persistent record of the changes made by a task.

Changelog: A YAML file that describes job run with the changes made to the ensemble.

Secrets: An object that represents a secret value. Secrets are stored in a separate configuration file outside of version control or retrieved from a KMS such as Hashicorp Vault. Sensitive values and objects tainted by sensitive values are always redacted when serialized or logged.

Local values: Values and configurations settings that are dependent on the local environment and therefore should be saved separately from a shared repository or deployment history. For example, connection or proxy settings. Marking which settings are local helps enable a reproducible infrastructure.

External values: Values that are not directly serialized into the model but rather referenced by a name that is resolved during task execution. Examples are secrets, local values, file references, and external (imported) instances. External values can indicate if they've changed and so can participate in incremental updates of the ensemble.

External instances: External instances are instances that are part of a separate ensemble. They are only accessible through the `external` expression function unless the service template has corresponding node template with a `select` or `substitute` directive.

Dependencies: Tracking dependencies between instances enable the incremental update of ensembles. Dependencies can be inferred through the relationships defined in TOSCA service template, by the expressions that define an instance's properties and attributes, or dynamically by configurators using the Task API.
