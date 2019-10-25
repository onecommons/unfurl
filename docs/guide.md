## Basic functionality and concepts

The manifest defines arbitrary and abstract representations of resources using locally defined identifiers. The goal is to make it easy to build humane specifications.

Git repository that contains YAML files that specifies how resources should be configured.

Unfurl relies on other tools and services to actually do the work, they are encapsulated in a "Configurator" interface, which tries to impose the minimum requirements and semantics.

Running Unfurl attempts to apply that specification and commits the results of that run. The exact versions of all external assets and tools utilized are recorded so playback can be fully reproducible.

Easily express dependencies and requirements between arbitrary high-level services in a consistent manner.

## Design

### Metadata

Use of attributes:
* Mechanism to save state for private use
* means of passing values between configurations:
  - as parameter references
  - shared semantics
* As documentation

### Resources
  * Have local name for reference in config file
  * Have desired state vs. observed state
  * Have identity tied to connection type
  * value of attributes: a YAML datatype or resource reference

### Assumptions / design principles:
  * a resource is in state X after manifest at commit X was applied successfully
  * open world: omission of configuration A from manifest doesn't means it isn't installed on resource
  * but by default assumes changes weren't made to declared configurations
  * deterministic: all resources will have the same state if commit X was applied
  * a configuration retrieved with particular commit id and a set of parameter values
    will deterministically produce the same result when applied to a resource in a given state
  * state X = (configuration A + configuration B)
  * what about failure states?
  * configuration actions either succeed or fail

## Comparisons to other tools

### Ansible

Unfurl shares many similarities with Ansible; in fact it relies on Ansible as a library. You can think of it as a declarative wrapper around Ansible. Because it records the history of operations that were previously applied, it can much more efficiently apply incremental updates.

### Terraform

Terraform's design shares many similarities to Unfurl but is more ambitious in that it attempts to calculate an update plan by generating a diff between the current specification and current state. This requires resource plugins to implement full CRUD semantics for managing resources and implement a fairly complex interface in Go.

Unlike Terraform, Unfurl maintains a history of configuration changes -- enabling it to support much simpler semantics for resources. This way they can be defined in a simple and ad hoc manner and using just YAML configuration DSL or through a simple Python API as opposed to relying on Go developer with domain expertise building a resource plugin.

## Change algebra
spec:
 the latest spec

status:

contains the current deployed state:
attributes, capabilities, runtime dependencies, deployed artifacts
Each resource also links the last node template version applied

dependencies and artifacts record with configuration last updated them

configchanged: inputs and attributes on each node
* configuration tasks form a graph of dependencies between nodes 
that the configuration depends on.
* this forms the execution plan graph for each job
* when a dependency changes, the associated configuration will be rerun 

configurations:

old subplans aren't compatible with the current state if depends on
resources that no longer compatible.

also if they produce results that will no longer be compatible
 
