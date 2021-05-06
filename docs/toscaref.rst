================================
TOSCA Service Template Reference
================================

An application in Unfurl is described in a service template and its Domain Specific Language (DSL) is based on a standard called TOSCA.

Service templates are written in YAML and describe the logical representation of an application, which we call a `topology`. In a service template, you can describe the application's components, how they relate to one another, how they are installed and configured and how they're monitored and maintained.

Other than the YAML itself, a service template can comprise multiple resources such as configuration and installation scripts (or Puppet Manifests, or Chef Recipes, etc..), code, and basically any other resource you require for running your application.

All files in the directory that contains the service template's main file, are also considered part of the service template, and paths described in the service template are relative to that directory. 


A service template is comprised of several high level sections:


.. toctree::
   :maxdepth: 2

   DSL Definitions<toscaref/spec-dsldef.rst>
   Requirements<toscaref/spec-requirements.rst>
   Node Types<toscaref/spec-node-types.rst>
   Node Templates<toscaref/spec-node-templates.rst>
   Data Types<toscaref/spec-data-types.rst>
   Inputs<toscaref/spec-inputs.rst>
   Outputs<toscaref/spec-outputs.rst>
   Groups<toscaref/spec-groups.rst>
   Imports<toscaref/spec-imports.rst>
   Interfaces<toscaref/spec-interfaces.rst>

|
|
















