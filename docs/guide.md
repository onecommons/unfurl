## Introduction

Unfurl coordinates the deployment of disparate configuration management and build tools and records the results in a git repository.

Unfurl lets you mix and match both declarative and imperative approaches in the same project, and both carefully designed, fine-grained type system or course-grained objects can live alongside ad-hoc metadata and dynamically generated configuration.

The core of an Unfurl project is a YAML manifest file that includes both a specification of the intended outcome and the status of the current live instances. The specification is defined using OASIS's [TOSCA](tosca) 1.3 ("Topology and Orchestration Specification for Cloud Applications") standard and the status is presented as an hierarchy of the operational status and attributes of live resources created, modified or observed by deploying and managing the project.

As illustrated in the figure below, the fundamental operation of Unfurl is to apply the specified configuration to a set of resources and record the results.

Unfurl maintains a change log recording what operations were applied to which resources and how those resources where changed. The changes can also be committed to git automatically so that each commit represents an update to the state of the system.

![diagram](diagram1.svg)

## Unfurl vs. ...

### Ansible

Unfurl shares many similarities with Ansible; in fact it relies on Ansible as a library. You can think of it as a declarative wrapper around Ansible. Because it records the history of operations that were previously applied, it can much more efficiently apply incremental updates.

### Terraform

Terraform's design shares many similarities to Unfurl but Terraform is more complex in that it attempts to calculate an update plan by generating a diff between the current specification and current state. This requires resource plugins to implement full CRUD semantics for managing resources and implement a fairly complex interface in Go. Unlike Terraform, Unfurl maintains a history of configuration changes -- enabling it to support much simpler semantics for updating resources. This way resources can be defined in a simple and ad hoc manner, either through YAML configuration or a relatively simple Python API as opposed to the significant domain expertise need to build a Terraform resource plugin.

Both Unfurl and Terraform can be thought of as cloud provider agnostic but unlike Terraform -- which requires separate configuration for each cloud provider -- Unfurl lets you write specifications that can be applied without change because TOSCA provides a type system that enables abstract topologies.

Unfurl's deep integration with git and external repositories provides significantly different approach to sharing, integration and composability. Terraform support for sharing configuration outputs and state files is limited to its proprietary extensions, Terraform Cloud and Terraform Enterprise.

### TOSCA Orchestrators (e.g. Cloudify, Ystia Orchestrator)

Unlike other [TOSCA Orchestrators](https://wiki.oasis-open.org/tosca/TOSCA-implementations), Unfurl doesn't require a server component and more generally isn't intended to manage very large infrastructure deployments -- instead the expectation would be that Unfurl coordinates the setup and configuration of a dynamic orchestrator (mostly likely Kubernetes).

## Unfurl Concepts

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

Configurators: A software driver that implements an operation. Configurators apply changes to instances. There are built-in configurators for shell scripts, Ansible playbooks, Terraform configurations, and Kubernetes resources or you can include your as part of the service specification.

Installations: Installation can create and update many instances as a side effect of its operation and only the important ones need to be reified. See Helm Release as an example invitation.

Installers: A node template represents the software that creates and manages installations. Installers allow implementations to be reified as first-class instances so they can be instantiated and configured themselves, enabling the local client environment to be bootstrapped as well.

Ref expressions:

Job: A jobs executes a workflow by instantiating a plan of tasks to run.

Tasks: Instantiates a configurator to carry out the given `operation`. It's results are

ConfigChange: a persistent record of the changes made by a task.

Changelog: A config file that containing the history of jobs and config changes made to an ensemble.

Secrets: An object that represents a secret value. Secrets are stored in a separate configuration file outside of version control or retrieved from a KMS such as Hashicorp Vault. Sensitive values and objects tainted by sensitive values are always redacted when serialized or logged.

Local values: Values and configurations settings that are dependent on the local environment and therefore should be saved separately from a shared repository or deployment history. For example, connection or proxy settings. Marking which settings are local helps enable a reproducible infrastructure.

External values: Values that are not directly serialized into the model but rather referenced by a name that is resolved during task execution. Examples are secrets, local values, file references, and external (imported) instances. External values can indicate if they've changed and so can participate in incremental updates of the ensemble.

External instances: External instances are instances that are part of a separate ensemble. They are only accessible through the `external` expression function unless the service template has corresponding node template with a `select` or `substitute` directive.

Dependencies: Tracking dependencies between instances enable the incremental update of ensembles. Dependencies can be inferred through the relationships defined in TOSCA service template, by the expressions that define an instance's properties and attributes, or dynamically by configurators using the Task API. 

## Processing Model

The core behavior of Unfurl is to run a `job` that executes a `workflow` on a given topology instance.
There are two fundamental workflows ("normative workflows" in TOSCA terminology):
`deploy`, which installs the topology, and `undeploy`, which uninstalls it.

When a workflow job is run, it updates the status of its affected instances.
Each `instance` represented in a manifest has a status that indicates
its relationship to its specification:

Unknown
OK
Degraded
Error
Pending
Absent

There are also `check` and `discover` workflows which update the status of
instances the based on their current live state.
Users can also define custom workflows but they do not affect the change history of the topology.

When a workflow is applied to an instance it will be skipped if it already has
the desired status (either "OK" or "Absent"). If its status is `Unknown`,
`check` will be run first. Otherwise the workflow will be applied by executing one or more `operations` on a target instance.

If it succeeds, the target instance status will be set to either `OK` or `Absent`
for `deploy` and `undeploy` respectively. If it fails, the status will depend on whether the instance was modified by the operation.
If it has been, the status is set to `Error`; if the operation didn't report whether or not it was modified, it is set to `Unknown`. Otherwise, the status won't be changed.

\_ 1: or to Degraded, depending the priority of the task.

### Operations and tasks

The actual work is done by `operations` and as they are executed the `node state` of the target instance is updated.
Nodes states include: `initial`, `creating`, `created`, `configuring`, `configured`,
`starting`, `started`, `stopping`, `deleting`, and `error`.
See
[TOSCA 1.3, §3.4.1](https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/cos01/TOSCA-Simple-Profile-YAML-v1.3-cos01.html#_Toc454457724) for a complete definitions

Each `task` in a `job` corresponds to an operation that was executed and is assigned a
`changeid`. Each task is recorded in the job's `changelog` as a `ConfigChange`,
which designed so that it can replayed to reproduce the instance.

Instances keep track of the last operation that was applied to it and also of the last
task that observed changes to the internal state of the instance (which may or may not be
reflected in attributes exposed in the topology model). Tracking internal state
is useful because dependent instances may need to know when it has changed and to know
if it is safe to delete an instance.

When status of an instance is saved in the manifest, the attributes described above
can be found in its `readyState`, for example:

```yaml
readyState:
  local: ok # the explicit status of this instance
  effective: ok # its status with its dependencies' statuses considered
  state: started # operating state
lastConfigChange: 99 # change id of the last ConfigChange that was applied
lastStateChange: 80
```

## Imports

The `imports` section of the manifest lets you declare instances that are imported from external manifests. Instances listed here can be accessed in two ways: One, they will be implicitly used if they match a node template that is declared abstract using the "select" directive (see "3.4.3 Directives"). Two, they can be explicitly referenced using the `external` expression function.

There are 3 instances that are always implicitly imported even if they are not declared:

- The `localhost` instance that represents the machine Unfurl is currently executing on. This instance is accessed through the `ORCHESTRATOR` keyword in TOSCA and is defined in the home manifest that resides in your Unfurl home folder.

- The `local` and `secret` instances are for representing properties that may be specific to the local environment that the manifest is running in and are declared in the local configuration file. All `secret` attribute values are treated as `sensitive` and redacted when necessary. These instances can be accessed through the `local` and `secret` expression functions (which are just shorthands for the `external` function).
