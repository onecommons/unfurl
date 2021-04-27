================================
TOSCA Service Template Reference
================================

.. contents::

An application in Unfurl is described in a service template and its Domain Specific Language (DSL) is based on a standard called TOSCA.

Service templates are written in YAML and describe the logical representation of an application, which we call a `topology`. In a service template, you can describe the application's components, how they relate to one another, how they are installed and configured and how they're monitored and maintained.

Other than the YAML itself, a service template can comprise multiple resources such as configuration and installation scripts (or Puppet Manifests, or Chef Recipes, etc..), code, and basically any other resource you require for running your application.

All files in the directory that contains the service template's main file, are also considered part of the service template, and paths described in the service template are relative to that directory.

A service template is comprised of several high level sections:


Dsl Definitions
+++++++++++++++

The dsl_definitions section can be used to define arbitrary data structures that can then be reused in different parts of the service templates using YAML anchors and aliases. 

Inputs
++++++

Inputs are parameters injected into the service template upon deployment creation/initiation. These parameters can be referenced by using the get_input (spec-intrinsic-functions.md#get-input) intrinsic function.

Inputs are useful when there's a need to inject parameters to the service template which were unknown when the service template was created and can be used for distinction between different deployments of the same service template.

Outputs
++++++++

Outputs provide a way of exposing global aspects of a deployment. When deployed, a service template can expose specific outputs of that deployment - for instance, an endpoint of a server or any other runtime or static information of a specific resource.

Imports
++++++++

Imports allow the author of a service template to reuse service template files or parts of them and use predefined types (e.g. from the types.yaml file).

Groups
+++++++

Groups provide a way of configuring shared behavior for different sets of`node_templates.

Interfaces
++++++++++

Interfaces provide a way to map logical tasks to executable operations.

Relationships
+++++++++++++

Relationships let you define how nodes relate to one another. For example, a web_server node can be contained_in a vm node or an application node can be connected_to a database node.

Data Types
++++++++++

data_types are useful for grouping together and re-using a common set of properties, along with their types and default values.

Node Types
++++++++++

node_types are used for defining common properties and behaviors for node-templates. node-templates can then be created based on these types, inheriting their definitions.

Node Templates
++++++++++++++

node_templates represent the actual instances of node types which would eventually represent a running application/service as described in the service template.

For more info see :doc:`toscaref/spec-node-templates`
