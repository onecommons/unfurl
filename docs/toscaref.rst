TOSCA Service Template Reference
=================================

Overview
++++++++

An application in Unfurl is described in a service template and its Domain Specific Language (DSL) is based on a standard called TOSCA.

Service templates are written in YAML and describe the logical representation of an application, which we call a `topology`. In a service template, you can describe the application's components, how they relate to one another, how they are installed and configured and how they're monitored and maintained.

Other than the YAML itself, a service template can comprise multiple resources such as configuration and installation scripts (or Puppet Manifests, or Chef Recipes, etc..), code, and basically any other resource you require for running your application.

All files in the directory that contains the service template's main file, are also considered part of the service template, and paths described in the service template are relative to that directory.



.. toctree::
   :maxdepth: 1

   toscaref/sections.rst
   spec-node-templates.md

