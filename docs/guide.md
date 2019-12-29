## Introduction

Unfurl coordinates deployment of heterogenous configuration management and build tools and records the results in a git repository.

Unfurl lets you mix and match both declarative and imperative approaches in the same project, and both carefully designed, fine-grained type system or course-grained objects can live alongside ad-hoc metadata and dynamically generated configuration.

The core of an Unfurl project is a YAML manifest file that includes both a specification of the intended outcome and the status of the current live instances. The specification is defined using the TOSCA 1.2 OASIS standard ("Topology and Orchestration Specification for Cloud Applications") and the status is presented as an hierarchy of the operational status and attributes of live resources created, modified or observed by deploying and managing the project. 

Unfurl maintains change log recording a history of the operations and changes that were applied to them. 

## Conceptual model and glossary

### From the TOSCA Specification:

Service Template

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

### Unfurl Model

manifest

Operational Instances:

Resources:

Configurators: A software driver that implements an operation. Shell, Ansible, Terraform or they can Kubernetes.

Installers: A node template that declares a mapping of operations to configurators. As first-class node templates they can be instantiated and configured themselves, enabling the local client environment to be managed as well.

Installations: Installation can create and update many as a side effect of its operation and only the important ones need to be reified. Installations also provide a 

Ref expressions

Job Executes a workflow by instantiating a plan of tasks to run. 

Tasks: Instantiates a configurator to carry out the given `operation`. It's results are

ConfigChange: a persistent record of the changes made by a task.

Changelog

Secrets: An object that represents a secret value. Secrets are stored in a separate configuration file outside of version control or retrieved from a KSM such as Hashicorp Vault. Sensitive values and objects tainted by sensitive values are always redacted when written out. 

Local values: Values and configurations settings that are dependent on the local environment and therefore should be saved separately from a shared repository or deployment history, for example, proxy settings. Delineating these helps enable a reproducible infrastructure.

External values: A reference to an object that is not modeled by the service template but instances are still dependent on. Examples would be a local file that may change or references to resources defined in another manifest.

Dependencies: Configuration dependencies between instances expressed as `Ref expressions`. TOSCA relationships specify dependencies between nodes but specific dependencies 

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
NotPresent

There are also `check` and `discover` workflows which update the status of 
instances the based on their current live state. 
Users can also define custom workflows but they do not affect the change history of the topology.

When a workflow is applied to an instance it will be skipped if it already has 
the desired status (either "OK" or "NotPresent"). If its status is `Unknown`, 
`check` will be run first. Otherwise the workflow will be applied by executing one or more `operations` on a target instance.

If it succeeds, the target instance status will be set to either `OK` or `NotPresent`
for `deploy` and `undeploy` respectively. If it fails, the status will depend on if the instance was modified by the operation.
If they have, the status is set to error; if they didn't report whether they did or not, it is set to `Unknown`. Otherwise, the status won't be changed.

_ 1: or to Degraded, depending the priority of the task.

### Operations and tasks 

The actual work is done by `operations` and as they are executed the `node state` of the target instance is updated.
Nodes states include: `initial`, `creating`, `created`, `configuring`, `configured`, 
`starting`, `started`, `stopping`, `deleting`, and `error`. 
See section 3.4.1, p74 of the TOSCA 1.3 Spec. for a complete definitions.

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

## Comparisons to other tools

### Ansible

Unfurl shares many similarities with Ansible; in fact it relies on Ansible as a library. You can think of it as a declarative wrapper around Ansible. Because it records the history of operations that were previously applied, it can much more efficiently apply incremental updates.

### Terraform

Terraform's design shares many similarities to Unfurl but Terraform is more ambitious in that it attempts to calculate an update plan by generating a diff between the current specification and current state. This requires resource plugins to implement full CRUD semantics for managing resources and implement a fairly complex interface in Go.

Unlike Terraform, Unfurl maintains a history of configuration changes -- enabling it to support much simpler semantics for resources. This way they can be defined in a simple and ad hoc manner and using just YAML configuration DSL or through a simple Python API as opposed to relying on Go developer with domain expertise building a resource plugin.
